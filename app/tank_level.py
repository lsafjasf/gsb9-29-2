"""Call site 1 (refactored): liquid level h of a partially filled tank."""

import math

from solver import RootProblem, StopReason, solve


def find_level(target_volume, radius=1.0, length=2.0, tol=1e-6, max_iter=100):
    def volume_minus_target(h):
        if h < 0.0 or h > 2.0 * radius:
            raise ValueError("h out of tank range")
        z = radius - h
        filled_area = radius * radius * math.acos(z / radius) - z * math.sqrt(
            max(0.0, 2.0 * radius * h - h * h)
        )
        return length * filled_area - target_volume

    problem = RootProblem(
        f=volume_minus_target, bracket=(0.0, 2.0 * radius)
    )
    result = solve(problem, tol=tol, max_iter=max_iter, method="bisection")

    if result.reason is StopReason.CONVERGED:
        return "ok", result.x, result.residual
    if result.reason is StopReason.MAX_ITER:
        return "no-convergence", result.x, result.residual
    if result.reason is StopReason.DIVERGED:
        return "diverged", result.x, result.residual
    return "bad-input", None, None
