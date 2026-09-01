"""TEP-Guard: serverless multivariate process monitoring on the Tennessee Eastman benchmark."""

from .monitor import PCAMonitor, MonitorResult
from .data import (
    FAULTS,
    DETECTABLE_FAULTS,
    UNDETECTABLE_FAULTS,
    ISOLATION_SCORED_FAULTS,
    VARIABLE_TAGS,
    VARIABLE_NAMES,
    describe_variable,
    load_training,
    load_test,
)

__version__ = "0.1.0"
__all__ = [
    "PCAMonitor",
    "MonitorResult",
    "FAULTS",
    "DETECTABLE_FAULTS",
    "UNDETECTABLE_FAULTS",
    "ISOLATION_SCORED_FAULTS",
    "VARIABLE_TAGS",
    "VARIABLE_NAMES",
    "describe_variable",
    "load_training",
    "load_test",
]
