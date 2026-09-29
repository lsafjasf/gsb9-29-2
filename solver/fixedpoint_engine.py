"""Fixed-point engine implementing the unified contract.

Iteration count: number of updates x <- g(x).
Convergence criterion: |g(x) - x| <= tol (absolute step residual).
The returned residual on CONVERGED is |g(x*) - x*| evaluated at x*,
matching the legacy successful path exactly.
"""

from solver._common import blew_up, diverged, finite_real, invalid, validate_controls
from solver.types import SolveResult, StopReason


def solve_fixedpoint(problem, tol, max_iter):
    control_error = validate_controls(tol, max_iter)
    if control_error is not None:
        return invalid(control_error)
    if problem.g is None:
        return invalid("fixed-point requires problem.g")
    if problem.x0 is None:
        return invalid("fixed-point requires problem.x0")
    if not finite_real(problem.x0):
        return invalid("x0 must be a finite real number")

    x = float(problem.x0)

    for n in range(1, max_iter + 1):
        y = float(problem.g(x))
        step = abs(y - x)
        x = y
        if not (x == x):
            return diverged(x, n, "g produced NaN")
        if step <= tol:
            residual = abs(float(problem.g(x)) - x)
            return SolveResult(
                StopReason.CONVERGED, x, residual, n, "step within tol"
            )
        if blew_up(x):
            return diverged(x, n, "sequence left the blow-up guard")

    residual = abs(float(problem.g(x)) - x)
    return SolveResult(
        StopReason.MAX_ITER, x, residual, max_iter, "iteration limit reached"
    )
