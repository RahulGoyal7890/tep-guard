"""Loading and describing the Tennessee Eastman Process benchmark data.

Source: Downs & Vogel (1993), "A plant-wide industrial process control
problem", Computers & Chemical Engineering 17(3):245-255. The .dat files
used here are the widely redistributed simulation runs from the Braatz
group at the University of Illinois.

File layout, which is easy to get wrong:

    d00.dat      (52, 500)   fault-free TRAINING run -- TRANSPOSED
    d01..d21.dat (480, 52)   faulty training runs
    d00_te.dat   (960, 52)   fault-free TEST run
    dNN_te.dat   (960, 52)   faulty test runs, fault active from sample 160

Sampling interval is 3 minutes. In the faulty test runs the first 160
samples (8 hours) are normal operation and the fault is introduced at
index 160, so index 160 is the first faulty sample.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

SAMPLE_INTERVAL_MINUTES = 3
FAULT_ONSET_INDEX = 160
N_VARIABLES = 52

# --------------------------------------------------------------------------
# Variable names. Columns 0-40 are XMEAS(1..41), columns 41-51 are XMV(1..11).
# XMV(12) (agitator speed) is constant in these runs and is not included.
# --------------------------------------------------------------------------

XMEAS_NAMES = [
    "A feed (stream 1)",
    "D feed (stream 2)",
    "E feed (stream 3)",
    "A and C feed (stream 4)",
    "Recycle flow (stream 8)",
    "Reactor feed rate (stream 6)",
    "Reactor pressure",
    "Reactor level",
    "Reactor temperature",
    "Purge rate (stream 9)",
    "Product separator temperature",
    "Product separator level",
    "Product separator pressure",
    "Product separator underflow (stream 10)",
    "Stripper level",
    "Stripper pressure",
    "Stripper underflow (stream 11)",
    "Stripper temperature",
    "Stripper steam flow",
    "Compressor work",
    "Reactor cooling water outlet temperature",
    "Separator cooling water outlet temperature",
    "Reactor feed analysis A",
    "Reactor feed analysis B",
    "Reactor feed analysis C",
    "Reactor feed analysis D",
    "Reactor feed analysis E",
    "Reactor feed analysis F",
    "Purge gas analysis A",
    "Purge gas analysis B",
    "Purge gas analysis C",
    "Purge gas analysis D",
    "Purge gas analysis E",
    "Purge gas analysis F",
    "Purge gas analysis G",
    "Purge gas analysis H",
    "Product analysis D",
    "Product analysis E",
    "Product analysis F",
    "Product analysis G",
    "Product analysis H",
]

XMV_NAMES = [
    "D feed flow valve (stream 2)",
    "E feed flow valve (stream 3)",
    "A feed flow valve (stream 1)",
    "A and C feed flow valve (stream 4)",
    "Compressor recycle valve",
    "Purge valve (stream 9)",
    "Separator pot liquid flow valve (stream 10)",
    "Stripper liquid product flow valve (stream 11)",
    "Stripper steam valve",
    "Reactor cooling water flow valve",
    "Condenser cooling water flow valve",
]

VARIABLE_TAGS = [f"XMEAS({i})" for i in range(1, 42)] + [
    f"XMV({i})" for i in range(1, 12)
]
VARIABLE_NAMES = XMEAS_NAMES + XMV_NAMES

assert len(VARIABLE_TAGS) == len(VARIABLE_NAMES) == N_VARIABLES


def describe_variable(index: int) -> str:
    """Human-readable label for a zero-based column index."""
    return f"{VARIABLE_TAGS[index]} {VARIABLE_NAMES[index]}"


# --------------------------------------------------------------------------
# Fault catalogue
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Fault:
    number: int
    description: str
    kind: str
    # Zero-based column indices that the process engineering says should move
    # first. Empty when the root cause is unknown or too diffuse to attribute.
    # See ISOLATION_GROUND_TRUTH_NOTES in the README before trusting these.
    root_variables: tuple[int, ...] = ()


def _idx(tag: str) -> int:
    return VARIABLE_TAGS.index(tag)


FAULTS: dict[int, Fault] = {
    0: Fault(0, "Normal operation", "none"),
    1: Fault(
        1,
        "A/C feed ratio, B composition constant (stream 4)",
        "step",
        (_idx("XMEAS(4)"), _idx("XMV(4)"), _idx("XMEAS(1)")),
    ),
    2: Fault(2, "B composition, A/C ratio constant (stream 4)", "step"),
    3: Fault(3, "D feed temperature (stream 2)", "step"),
    4: Fault(
        4,
        "Reactor cooling water inlet temperature",
        "step",
        (_idx("XMV(10)"), _idx("XMEAS(21)")),
    ),
    5: Fault(
        5,
        "Condenser cooling water inlet temperature",
        "step",
        (_idx("XMV(11)"), _idx("XMEAS(22)")),
    ),
    6: Fault(
        6,
        "A feed loss (stream 1)",
        "step",
        (_idx("XMEAS(1)"), _idx("XMV(3)")),
    ),
    7: Fault(
        7,
        "C header pressure loss, reduced availability (stream 4)",
        "step",
        (_idx("XMEAS(4)"), _idx("XMV(4)")),
    ),
    8: Fault(8, "A, B, C feed composition (stream 4)", "random variation"),
    9: Fault(9, "D feed temperature (stream 2)", "random variation"),
    10: Fault(10, "C feed temperature (stream 4)", "random variation"),
    11: Fault(
        11,
        "Reactor cooling water inlet temperature",
        "random variation",
        (_idx("XMV(10)"), _idx("XMEAS(21)")),
    ),
    12: Fault(
        12,
        "Condenser cooling water inlet temperature",
        "random variation",
        (_idx("XMV(11)"), _idx("XMEAS(22)")),
    ),
    13: Fault(13, "Reaction kinetics", "slow drift"),
    14: Fault(
        14,
        "Reactor cooling water valve",
        "sticking",
        (_idx("XMV(10)"), _idx("XMEAS(21)"), _idx("XMEAS(9)")),
    ),
    15: Fault(
        15,
        "Condenser cooling water valve",
        "sticking",
        (_idx("XMV(11)"), _idx("XMEAS(22)")),
    ),
    16: Fault(16, "Unknown", "unknown"),
    17: Fault(17, "Unknown", "unknown"),
    18: Fault(18, "Unknown", "unknown"),
    19: Fault(19, "Unknown", "unknown"),
    20: Fault(20, "Unknown", "unknown"),
    21: Fault(
        21,
        "Stream 4 valve fixed at steady state position",
        "constant position",
        (_idx("XMEAS(4)"), _idx("XMV(4)")),
    ),
}

# Faults 3, 9 and 15 have no reported mean shift or variance change large
# enough to separate them from normal operation with any published method.
# They are excluded from headline detection numbers and reported separately,
# which is the convention in the FDD literature. Not excluding them is the
# usual way portfolio projects end up with a suspiciously low mean.
UNDETECTABLE_FAULTS = (3, 9, 15)

DETECTABLE_FAULTS = tuple(f for f in range(1, 22) if f not in UNDETECTABLE_FAULTS)

# Faults whose root cause maps to specific instrumentation without argument.
# Isolation accuracy is only reported on these.
ISOLATION_SCORED_FAULTS = tuple(
    f for f, spec in FAULTS.items() if f != 0 and spec.root_variables
)


# --------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------


def _resolve(data_dir: str | Path) -> Path:
    path = Path(data_dir)
    if not path.exists():
        raise FileNotFoundError(
            f"TEP data directory not found: {path}. "
            "Run `python scripts/download_data.py` first."
        )
    return path


def load_training(data_dir: str | Path = "data/raw") -> np.ndarray:
    """Fault-free training run. Returns (500, 52).

    d00.dat ships transposed as (52, 500) while every other file is
    (samples, variables). Loading it without the transpose silently gives
    you a 52-sample, 500-variable matrix, PCA still 'works', and every
    number downstream is garbage. This is the single most common TEP bug.
    """
    raw = np.loadtxt(_resolve(data_dir) / "d00.dat")
    if raw.shape == (N_VARIABLES, 500):
        raw = raw.T
    elif raw.shape != (500, N_VARIABLES):
        raise ValueError(f"Unexpected shape for d00.dat: {raw.shape}")
    return np.ascontiguousarray(raw, dtype=np.float64)


def load_test(fault: int, data_dir: str | Path = "data/raw") -> np.ndarray:
    """Test run for a given fault number. Returns (960, 52).

    For fault > 0 the fault is active from FAULT_ONSET_INDEX onward.
    """
    if fault not in FAULTS:
        raise KeyError(f"Unknown fault number: {fault}")
    arr = np.loadtxt(_resolve(data_dir) / f"d{fault:02d}_te.dat")
    if arr.shape != (960, N_VARIABLES):
        raise ValueError(f"Unexpected shape for d{fault:02d}_te.dat: {arr.shape}")
    return np.ascontiguousarray(arr, dtype=np.float64)


def fault_labels(n_samples: int = 960, fault: int = 1) -> np.ndarray:
    """Boolean ground truth: True where the fault is active."""
    labels = np.zeros(n_samples, dtype=bool)
    if fault != 0:
        labels[FAULT_ONSET_INDEX:] = True
    return labels


def samples_to_minutes(n_samples: float) -> float:
    return n_samples * SAMPLE_INTERVAL_MINUTES
