"""Shared types for the unified solver interface.

Stop reasons form a closed enum so callers never have to string-match or
inspect exception types:

CONVERGED     |f(x*)| <= tol
MAX_ITER      the iteration limit was reached without converging
DIVERGED      the sequence became non-finite, crossed the blow-up guard,
              the derivative vanished, or the problem function raised a
              numerical/domain error mid-iteration
INVALID_INPUT bad arguments (finite reals required, tol > 0, max_iter >= 1,
              callable functions, a valid bracketing interval, ...)
"""

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional


class StopReason(Enum):
    CONVERGED = "converged"
    MAX_ITER = "max_iter"
    DIVERGED = "diverged"
    INVALID_INPUT = "invalid_input"


@dataclass(frozen=True)
class RootProblem:
    """Description of the equation to solve.

    f
        Residual function; we seek x with f(x) = 0. Always required.
    fp
        Derivative of f (Newton only).
    bracket
        Pair (a, b) with f(a)*f(b) <= 0 (bisection only).
    x0
        Initial guess (Newton / fixed-point).
    g
        Fixed-point map x -> g(x); its residual is g(x) - x. If supplied,
        ``f`` defaults to g(x) - x so every problem exposes one residual.
    """

    f: Optional[Callable[[float], float]] = None
    fp: Optional[Callable[[float], float]] = None
    bracket: Optional[tuple] = None
    x0: Optional[float] = None
    g: Optional[Callable[[float], float]] = None


@dataclass(frozen=True)
class SolveResult:
    reason: StopReason
    x: Optional[float]
    residual: Optional[float]
    iterations: int
    message: str

    @property
    def converged(self):
        return self.reason is StopReason.CONVERGED
