"""Legacy fixed-point iteration solver (pre-refactor). Do not use in new code.

Quirks preserved verbatim:
- stateful class; ``solve`` returns an integer status code
  (0=ok, 1=limit, 2=diverged, 3=bad input) instead of raising.
- on success the residual is |g(x)-x| evaluated at the returned point (one
  extra evaluation of g); on overflow it is the *previous* step size, so the
  two residuals are defined one step apart.
- hard-coded blow-up threshold |x| > 1e12.
"""

import math


class FixedPointSolver:
    CODE_OK = 0
    CODE_LIMIT = 1
    CODE_DIVERGED = 2
    CODE_BAD_INPUT = 3

    def __init__(self, g):
        if not callable(g):
            raise TypeError("g must be callable")
        self.g = g

    def solve(self, x0, maxit=100, tol=1e-7):
        if not isinstance(x0, (int, float)) or isinstance(x0, bool):
            return self.CODE_BAD_INPUT, x0, float("nan")
        if not math.isfinite(x0):
            return self.CODE_BAD_INPUT, x0, float("nan")

        x = float(x0)
        r = float("nan")
        for _ in range(maxit):
            y = self.g(x)
            r = abs(y - x)
            x = y
            if r <= tol:
                return self.CODE_OK, x, abs(self.g(x) - x)
            if not math.isfinite(x) or abs(x) > 1e12:
                return self.CODE_DIVERGED, x, r

        return self.CODE_LIMIT, x, r
