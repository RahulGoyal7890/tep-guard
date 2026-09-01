"""Metrics for fault detection and isolation.

Four numbers matter to a plant, and they trade off against each other:

    detection rate    fraction of genuinely faulty samples flagged
    false alarm rate  fraction of normal samples flagged. The reason
                      monitoring systems get switched off by operators.
    detection delay   time from fault onset to a *sustained* alarm
    isolation         did the contribution plot point at the right instrument

Detection delay uses a sustained-alarm rule rather than first-alarm. A single
sample crossing a limit is noise; operators do not act on it, and scoring it
as a detection flatters the model.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np

from .data import FAULT_ONSET_INDEX, SAMPLE_INTERVAL_MINUTES

DEFAULT_CONSECUTIVE = 3


@dataclass
class DetectionMetrics:
    fault: int
    detection_rate: float
    false_alarm_rate: float
    detection_delay_minutes: float | None
    detected: bool

    def as_dict(self) -> dict:
        return asdict(self)


def false_alarm_rate(
    alarms: np.ndarray, onset: int = FAULT_ONSET_INDEX
) -> float:
    """Alarm fraction over the pre-fault portion of a run."""
    pre = np.asarray(alarms, dtype=bool)[:onset]
    return float(pre.mean()) if pre.size else 0.0


def detection_rate(alarms: np.ndarray, onset: int = FAULT_ONSET_INDEX) -> float:
    """Alarm fraction over the faulty portion of a run."""
    post = np.asarray(alarms, dtype=bool)[onset:]
    return float(post.mean()) if post.size else 0.0


def first_sustained_alarm(
    alarms: np.ndarray,
    onset: int = FAULT_ONSET_INDEX,
    consecutive: int = DEFAULT_CONSECUTIVE,
) -> int | None:
    """Index of the first of `consecutive` consecutive alarms at or after onset.

    Returns None if the fault is never sustained-detected.
    """
    a = np.asarray(alarms, dtype=bool)
    run = 0
    for i in range(onset, a.size):
        run = run + 1 if a[i] else 0
        if run >= consecutive:
            return i - consecutive + 1
    return None


def detection_delay_minutes(
    alarms: np.ndarray,
    onset: int = FAULT_ONSET_INDEX,
    consecutive: int = DEFAULT_CONSECUTIVE,
) -> float | None:
    idx = first_sustained_alarm(alarms, onset, consecutive)
    if idx is None:
        return None
    return float((idx - onset) * SAMPLE_INTERVAL_MINUTES)


def evaluate_detection(
    alarms: np.ndarray,
    fault: int,
    onset: int = FAULT_ONSET_INDEX,
    consecutive: int = DEFAULT_CONSECUTIVE,
) -> DetectionMetrics:
    delay = detection_delay_minutes(alarms, onset, consecutive)
    return DetectionMetrics(
        fault=fault,
        detection_rate=detection_rate(alarms, onset),
        false_alarm_rate=false_alarm_rate(alarms, onset),
        detection_delay_minutes=delay,
        detected=delay is not None,
    )


# ------------------------------------------------------------- isolation --


def topk_hit_rate(
    contributions: np.ndarray,
    root_variables: tuple[int, ...],
    k: int = 3,
) -> float:
    """Fraction of samples whose top-k contributors include a root variable.

    contributions : (n_samples, n_variables)
    """
    if not root_variables:
        return float("nan")
    C = np.atleast_2d(np.asarray(contributions, dtype=np.float64))
    if C.shape[1] < k:
        raise ValueError("Fewer variables than requested top-k")
    # argpartition is O(m) per row vs O(m log m) for a full sort.
    topk = np.argpartition(-C, kth=k - 1, axis=1)[:, :k]
    truth = np.asarray(root_variables, dtype=int)
    hits = np.isin(topk, truth).any(axis=1)
    return float(hits.mean())


def summarize(rows: list[dict], key: str) -> float:
    """Mean of a metric across faults, ignoring None and NaN."""
    vals = [
        r[key]
        for r in rows
        if r.get(key) is not None and not (isinstance(r[key], float) and np.isnan(r[key]))
    ]
    return float(np.mean(vals)) if vals else float("nan")


def median_of(rows: list[dict], key: str) -> float:
    vals = [
        r[key]
        for r in rows
        if r.get(key) is not None and not (isinstance(r[key], float) and np.isnan(r[key]))
    ]
    return float(np.median(vals)) if vals else float("nan")
