"""Newton engine implementing the unified contract.

Iteration count: number of Newton updates performed (evaluations of fp and
of f at the new point). The initial point x0 is iteration 0.
Convergence criterion: |f(x)| <= tol (absolute residual).
"""

from solver._common import blew_up, diverged, finite_real, invalid, validate_controls
from solver.types import SolveResult, StopReason


def solve_newton(problem, tol, max_iter):
    control_error = validate_controls(tol, max_iter)
    if control_error is not None:
        return invalid(control_error)
    if problem.fp is None:
        return invalid("newton requires problem.fp (derivative)")
    if problem.x0 is None:
        return invalid("newton requires problem.x0")
    if not finite_real(problem.x0):
        return invalid("x0 must be a finite real number")

    x = float(problem.x0)
    try:
        fx = float(problem.f(x))
    except (ValueError, ArithmeticError) as exc:
        return invalid("f failed at x0: %s" % exc)

    if not (fx == fx):
        return diverged(x, 0, "f(x0) is NaN")
    if abs(fx) <= tol:
        return SolveResult(StopReason.CONVERGED, x, abs(fx), 0, "initial guess within tol")

    for n in range(1, max_iter + 1):
        try:
            d = float(problem.fp(x))
        except (ValueError, ArithmeticError) as exc:
            return diverged(x, n, "derivative raised: %s" % exc)
        if d == 0.0:
            return diverged(x, n, "derivative evaluated to zero")
        x = x - fx / d
        try:
            fx = float(problem.f(x))
        except (ValueError, ArithmeticError) as exc:
            return diverged(x, n, "f raised mid-iteration: %s" % exc)
        if not (fx == fx):
            return diverged(x, n, "f returned NaN")
        if abs(fx) <= tol:
            return SolveResult(
                StopReason.CONVERGED, x, abs(fx), n, "residual within tol"
            )
        if blew_up(x):
            return diverged(x, n, "newton sequence left the blow-up guard")

    return SolveResult(
        StopReason.MAX_ITER, x, abs(fx), max_iter, "iteration limit reached"
    )
