"""Bisection engine implementing the unified contract.

Iteration count: number of midpoint evaluations / interval halvings.
Convergence criterion: |f(mid)| <= tol (absolute residual), checked after
each midpoint evaluation.
"""

import math

from solver._common import diverged, invalid, validate_controls
from solver.types import SolveResult, StopReason


def solve_bisect(problem, tol, max_iter):
    control_error = validate_controls(tol, max_iter)
    if control_error is not None:
        return invalid(control_error)
    if problem.bracket is None:
        return invalid("bisection requires problem.bracket = (a, b)")
    if not (isinstance(problem.bracket, (tuple, list)) and len(problem.bracket) == 2):
        return invalid("bracket must be a pair (a, b)")

    a, b = problem.bracket
    if not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
        return invalid("bracket endpoints must be real numbers")
    if isinstance(a, bool) or isinstance(b, bool):
        return invalid("bracket endpoints must be real numbers")
    a, b = float(a), float(b)
    if not (math.isfinite(a) and math.isfinite(b)):
        return invalid("bracket endpoints must be finite")
    if not (a < b):
        return invalid("require a < b")

    try:
        fa = float(problem.f(a))
        fb = float(problem.f(b))
    except (ValueError, ArithmeticError) as exc:
        return invalid("f failed at a bracket endpoint: %s" % exc)

    if fa == 0.0:
        return SolveResult(StopReason.CONVERGED, a, 0.0, 0, "endpoint root")
    if fb == 0.0:
        return SolveResult(StopReason.CONVERGED, b, 0.0, 0, "endpoint root")
    if fa * fb >= 0.0:
        return invalid("f(a) and f(b) must have opposite signs")

    for n in range(1, max_iter + 1):
        c = (a + b) / 2.0
        try:
            fc = float(problem.f(c))
        except (ValueError, ArithmeticError) as exc:
            return diverged(c, n, "f raised mid-iteration: %s" % exc)
        if not (fc == fc):
            return diverged(c, n, "f returned NaN at a midpoint")
        if abs(fc) <= tol or fc == 0.0:
            return SolveResult(
                StopReason.CONVERGED, c, abs(fc), n, "residual within tol"
            )
        if fa * fc < 0.0:
            b, fb = c, fc
        else:
            a, fa = c, fc

    return SolveResult(
        StopReason.MAX_ITER, c, abs(fc), max_iter,
        "iteration limit reached",
    )
