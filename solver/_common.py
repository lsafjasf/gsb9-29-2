"""Internal helpers shared by the solver engines."""

import math

from solver.types import RootProblem, SolveResult, StopReason

BLOWUP = 1e12


def is_real_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def finite_real(value):
    return is_real_number(value) and math.isfinite(float(value))


def invalid(message):
    return SolveResult(StopReason.INVALID_INPUT, None, None, 0, message)


def diverged(x, iterations, message):
    residual = None
    if x is not None:
        residual = float("nan")
    return SolveResult(StopReason.DIVERGED, x, residual, iterations, message)


def validate_controls(tol, max_iter):
    if not finite_real(tol) or tol <= 0.0:
        return "tol must be a positive finite real number"
    if isinstance(max_iter, bool) or not isinstance(max_iter, int) or max_iter < 1:
        return "max_iter must be an integer >= 1"
    return None


def blew_up(x):
    return not math.isfinite(x) or abs(x) > BLOWUP
