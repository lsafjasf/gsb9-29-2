"""Call site 3 (legacy): fixed point of an equilibrium map.

Iterates x = g(x) and branches on the solver's integer status codes.
"""

import math

from legacy.fixedpoint_solver import FixedPointSolver


def find_equilibrium(relation=None, x0=0.5, maxit=100, tol=1e-7):
    if relation is None:
        relation = math.cos

    solver = FixedPointSolver(relation)
    code, x, residual = solver.solve(x0, maxit=maxit, tol=tol)
    if code == FixedPointSolver.CODE_OK:
        return "ok", x, residual
    if code == FixedPointSolver.CODE_LIMIT:
        return "limit", x, residual
    if code == FixedPointSolver.CODE_DIVERGED:
        return "diverged", x, residual
    return "invalid-start", x, residual
