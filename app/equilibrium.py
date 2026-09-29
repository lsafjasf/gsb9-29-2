"""Call site 3 (refactored): fixed point of an equilibrium map."""

import math

from solver import RootProblem, StopReason, solve


def find_equilibrium(relation=None, x0=0.5, tol=1e-7, max_iter=100):
    if relation is None:
        relation = math.cos

    problem = RootProblem(g=relation, x0=x0)
    result = solve(problem, tol=tol, max_iter=max_iter, method="fixedpoint")

    if result.reason is StopReason.CONVERGED:
        return "ok", result.x, result.residual
    if result.reason is StopReason.MAX_ITER:
        return "limit", result.x, result.residual
    if result.reason is StopReason.DIVERGED:
        return "diverged", result.x, result.residual
    return "invalid-start", x0, float("nan")
