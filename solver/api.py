"""Unified solver entry point."""

from solver._common import invalid
from solver.bisect_engine import solve_bisect
from solver.fixedpoint_engine import solve_fixedpoint
from solver.newton_engine import solve_newton
from solver.types import RootProblem, SolveResult, StopReason

_METHODS = ("auto", "bisection", "newton", "fixedpoint")


def _effective_problem(problem):
    if problem.f is not None or problem.g is None:
        return problem
    g = problem.g
    return RootProblem(
        f=lambda x: g(x) - x,
        fp=problem.fp,
        bracket=problem.bracket,
        x0=problem.x0,
        g=g,
    )


def solve(problem, tol=1e-8, max_iter=100, method="auto"):
    """Solve ``problem`` and always return a :class:`SolveResult`.

    Parameters
    ----------
    problem:
        RootProblem describing f(x) = 0 (or x = g(x)).
    tol:
        Absolute residual tolerance, in the same units as the residual.
        Convergence means |f(x)| <= tol (bisection/newton) or
        |g(x) - x| <= tol (fixed-point).
    max_iter:
        Maximum number of iterations, where an iteration is one midpoint
        evaluation (bisection) or one state update (newton/fixed-point).
        Iteration 0 is the initial point; it is never counted against the
        limit.
    method:
        "bisection", "newton", "fixedpoint" or "auto".
    """

    if not isinstance(problem, RootProblem):
        return invalid("problem must be a RootProblem instance")
    problem = _effective_problem(problem)
    if problem.f is None:
        return invalid("problem requires f (or g for fixed-point)")
    if not callable(problem.f):
        return invalid("problem.f must be callable")
    if method not in _METHODS:
        return invalid("method must be one of %s" % ", ".join(_METHODS))

    if method == "auto":
        if problem.fp is not None and problem.x0 is not None:
            method = "newton"
        elif problem.bracket is not None:
            method = "bisection"
        elif problem.g is not None and problem.x0 is not None:
            method = "fixedpoint"
        else:
            return invalid(
                "auto method selection needs fp+x0 (newton), bracket "
                "(bisection), or g+x0 (fixed-point)"
            )

    if method == "bisection":
        return solve_bisect(problem, tol, max_iter)
    if method == "newton":
        return solve_newton(problem, tol, max_iter)
    return solve_fixedpoint(problem, tol, max_iter)
