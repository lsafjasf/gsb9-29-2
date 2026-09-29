"""Legacy bisection solver (pre-refactor). Do not use in new code.

Quirks preserved verbatim:
- ``tol`` bounds the bracket *half-width* (units of x), it is NOT a residual.
- ``ValueError`` is raised both for invalid arguments and for domain errors
  raised by ``f`` while evaluating midpoints.
- On overflow a ``RuntimeError`` is raised; the last partial state is stashed
  on the exception object (``last_x`` / ``last_residual``).
"""

import math


def _is_real(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def bisect(f, a, b, tol=1e-6, maxit=100):
    if not callable(f):
        raise TypeError("f must be callable")
    if not (_is_real(a) and _is_real(b)) or not (
        math.isfinite(a) and math.isfinite(b)
    ):
        raise ValueError("bounds must be finite real numbers")
    if a >= b:
        raise ValueError("require a < b")

    fa = f(a)
    fb = f(b)
    if fa == 0.0:
        return float(a), 0.0
    if fb == 0.0:
        return float(b), 0.0
    if fa * fb >= 0.0:
        raise ValueError("f(a) and f(b) must have opposite signs")

    last_x = None
    last_residual = None
    for _ in range(maxit):
        c = (a + b) / 2.0
        fc = f(c)
        last_x, last_residual = c, fc
        if fc == 0.0 or (b - a) / 2.0 < tol:
            return c, fc
        if fa * fc < 0.0:
            b, fb = c, fc
        else:
            a, fa = c, fc

    err = RuntimeError(
        "bisection did not converge within %d iterations" % maxit
    )
    err.last_x = last_x
    err.last_residual = last_residual
    err.iterations = maxit
    raise err
