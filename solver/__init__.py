"""Unified numerical solver interface.

Public API:
    solve(problem, tol=1e-8, max_iter=100, method="auto") -> SolveResult

See docs/INTERFACE.md for the contract and docs/MAPPING.md for the legacy
parameter/return-value comparison.
"""

from solver.types import RootProblem, SolveResult, StopReason
from solver.api import solve

__all__ = ["RootProblem", "SolveResult", "StopReason", "solve"]
