"""nrt -- numerical regression testing framework (standard library only)."""

from .baseline import BaselineStore, CaseBaseline, utc_now_stamp
from .check import CaseResult, SubCheck, Tolerances, check_case
from .runner import Problem, measure
from .stats import Summary, degradation_limit, summarize, t_critical

__all__ = [
    "BaselineStore",
    "CaseBaseline",
    "CaseResult",
    "Problem",
    "SubCheck",
    "Summary",
    "Tolerances",
    "check_case",
    "degradation_limit",
    "measure",
    "summarize",
    "t_critical",
    "utc_now_stamp",
]
