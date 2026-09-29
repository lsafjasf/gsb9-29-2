"""Call site 1 (legacy): liquid level h of a partially filled tank.

Volume V of a cylinder (radius r, length L) filled to height h::

    V(h) = L * (r**2 * acos((r - h) / r)
                - (r - h) * sqrt(2*r*h - h**2))

Solve V(h) - target = 0 on [0, 2r] with bisection.
"""

import math

from legacy.bisect_solver import bisect


def find_level(target_volume, radius=1.0, length=2.0, tol=1e-6, maxit=100):
    def volume_minus_target(h):
        if h < 0.0 or h > 2.0 * radius:
            raise ValueError("h out of tank range")
        z = radius - h
        filled_area = radius * radius * math.acos(z / radius) - z * math.sqrt(
            max(0.0, 2.0 * radius * h - h * h)
        )
        return length * filled_area - target_volume

    try:
        h, residual = bisect(
            volume_minus_target, 0.0, 2.0 * radius, tol=tol, maxit=maxit
        )
    except ValueError:
        return "bad-input", None, None
    except RuntimeError as exc:
        return "no-convergence", exc.last_x, exc.last_residual
    return "ok", h, abs(residual)
