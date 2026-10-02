"""errreg: error-regression testing for numerical algorithms (stdlib only)."""

from .core import Tolerances, CaseResult, Baseline, DistSummary, PASS, FAIL, ERROR
from .runner import Case, Runner
from .baseline import save_baseline, load_baseline, has_baseline

__all__ = [
    "Tolerances", "CaseResult", "Baseline", "DistSummary",
    "PASS", "FAIL", "ERROR",
    "Case", "Runner",
    "save_baseline", "load_baseline", "has_baseline",
]
