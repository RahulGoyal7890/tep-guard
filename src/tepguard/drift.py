"""Detect when incoming data stops resembling what the model was trained on.

This is a different question from "is there a fault", and conflating the two
is how monitoring systems quietly rot.

    A FAULT is a deviation the model was built to catch. Something broke in
    the plant. The monitor is working correctly. Tell the operator.

    DRIFT is the model's assumptions expiring. The plant moved to a new
    operating point, an instrument was recalibrated, a feed composition
    changed permanently. Nothing is broken. The monitor is now wrong.
    Tell whoever owns the model.

They look identical from inside the monitor: both produce high alarm rates.
The difference is in the shape. A fault moves a few variables sharply and
leaves the rest alone. Drift shifts many variables mildly and persistently.

Getting this wrong is expensive in both directions. Treating drift as a fault
sends operators chasing equipment that is fine, and after a few false hunts
they stop trusting the system. Treating a fault as drift means silently
widening the limits until the monitor detects nothing at all -- the failure
mode where a dashboard stays green through an incident.

This module reports the evidence and deliberately does not decide. The
decision needs plant context the model does not have.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np

# A variable counts as shifted when its batch mean moves this far from the
# training mean, in training standard deviations. 1.0 is deliberately loose:
# normal operation wanders, and we only care about a broad simultaneous shift.
SHIFT_THRESHOLD = 1.0

# Fraction of variables that must shift before we call it broad. Set from the
# fault runs: a TEP fault typically moves under a fifth of the 52 variables
# beyond 1 sigma, so a third is comfortably above the fault signature.
BROAD_FRACTION = 0.33

# Variance ratio outside this band means the spread changed, not just the
# centre -- usually an instrument problem rather than an operating-point move.
VARIANCE_BAND = (0.5, 2.0)


@dataclass
class DriftReport:
    n_shifted: int
    step_change: bool
    fraction_shifted: float
    max_shift: float
    median_shift: float
    n_variance_changed: int
    broad_shift: bool
    verdict: str
    shifted_variables: list[int]

    def as_dict(self) -> dict:
        return asdict(self)


def _has_step_change(
    X: np.ndarray, monitor, threshold: float, window_fraction: float = 0.25
) -> bool:
    """True if the batch's opening window differs sharply from its closing one.

    Deliberately crude: an onset is a large, simultaneous, one-directional
    move, and a quarter-batch mean comparison catches that without needing
    changepoint detection in a Lambda.
    """
    n = X.shape[0]
    w = max(2, int(n * window_fraction))
    if n < 4 * 2:
        return False
    head = X[:w].mean(axis=0)
    tail = X[-w:].mean(axis=0)
    jump = np.abs(tail - head) / monitor.std_
    return bool((jump > threshold).sum() >= 0.10 * X.shape[1])


def check_drift(
    monitor,
    X: np.ndarray,
    shift_threshold: float = SHIFT_THRESHOLD,
    broad_fraction: float = BROAD_FRACTION,
) -> DriftReport:
    """Compare a batch's distribution against the monitor's training data.

    Uses the mean and standard deviation already stored on the fitted monitor,
    so this costs one pass over the batch and needs no reference data at
    runtime. That matters in Lambda, where holding the training set in memory
    to compare against would triple the image size for no benefit.
    """
    X = np.atleast_2d(np.asarray(X, dtype=np.float64))
    if X.shape[0] < 2:
        raise ValueError("Need at least two samples to assess drift")

    batch_mean = X.mean(axis=0)
    batch_std = X.std(axis=0, ddof=1)

    # Shift of each variable's centre, in training sigmas.
    shift = np.abs(batch_mean - monitor.mean_) / monitor.std_
    shifted = np.flatnonzero(shift > shift_threshold)

    # Change in spread. A variable whose centre held but whose noise doubled
    # is a different problem: often a failing sensor.
    ratio = (batch_std / monitor.std_) ** 2
    variance_changed = int(
        ((ratio < VARIANCE_BAND[0]) | (ratio > VARIANCE_BAND[1])).sum()
    )

    fraction = float(shifted.size / X.shape[1])

    # Count alone cannot separate a plant-wide fault from a plant-wide
    # operating-point change. Fault 6 (total A feed loss) cascades through
    # 39 of 52 variables -- by count it looks exactly like drift.
    #
    # What separates them is time. A fault has an onset: the batch is normal,
    # then it is not. Drift is already present when the batch starts. So
    # compare the opening window against the closing one. A large jump inside
    # the batch means an event happened; a flat profile at a shifted level
    # means the shift predates the batch.
    step = _has_step_change(X, monitor, shift_threshold)
    broad = fraction >= broad_fraction and not step

    if step and fraction >= broad_fraction:
        verdict = (
            "plant-wide deviation with a clear onset inside this batch: many "
            "variables moved, but they moved together at a point in time. "
            "That is a severe cascading fault, not model staleness."
        )
    elif broad:
        verdict = (
            "broad distribution shift: many variables moved together, which "
            "looks more like a new operating point than a fault. Review "
            "whether the model needs refitting before trusting its alarms."
        )
    elif shifted.size:
        verdict = (
            "localised deviation: a few variables moved, consistent with a "
            "process fault rather than model staleness."
        )
    else:
        verdict = "no significant distribution shift."

    if variance_changed > 0 and not broad:
        verdict += (
            f" {variance_changed} variable(s) also changed spread, which can "
            "indicate an instrument problem."
        )

    return DriftReport(
        n_shifted=int(shifted.size),
        step_change=step,
        fraction_shifted=round(fraction, 4),
        max_shift=round(float(shift.max()), 3),
        median_shift=round(float(np.median(shift)), 3),
        n_variance_changed=variance_changed,
        broad_shift=bool(broad),
        verdict=verdict,
        shifted_variables=[int(i) for i in shifted[:10]],
    )
