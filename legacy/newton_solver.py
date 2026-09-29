"""Legacy Newton solver (pre-refactor). Do not use in new code.

Quirks preserved verbatim:
- tolerance parameter is called ``eps`` and means |f(x)| <= eps.
- returns a dict ``{"x", "fx", "n"}`` on success, ``None`` on overflow.
- signals divergence with ``FloatingPointError`` and a zero derivative with
  ``ZeroDivisionError``; bad starting points raise ``ValueError``.
- hard-coded blow-up threshold |x| > 1e10.
"""

import math


def newton(f, fp, x0, max_iter=50, eps=1e-8):
    if not callable(f) or not callable(fp):
        raise TypeError("f and fp must be callable")
    if not isinstance(x0, (int, float)) or isinstance(x0, bool):
        raise ValueError("x0 must be a real number")
    if not math.isfinite(x0):
        raise ValueError("x0 must be finite")

    x = float(x0)
    fx = f(x)
    if abs(fx) <= eps:
        return {"x": x, "fx": fx, "n": 0}

    for n in range(1, max_iter + 1):
        d = fp(x)
        if d == 0.0:
            raise ZeroDivisionError("derivative evaluated to zero")
        x = x - fx / d
        fx = f(x)
        if abs(fx) <= eps:
            return {"x": x, "fx": fx, "n": n}
        if not math.isfinite(x) or abs(x) > 1e10:
            raise FloatingPointError("newton sequence diverged")

    return None
