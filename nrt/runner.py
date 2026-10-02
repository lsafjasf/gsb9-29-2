"""Measurement harness: run a problem, score it against its reference, time it."""

from __future__ import annotations

import time
from typing import List, Optional, Protocol, Tuple


class Problem(Protocol):
    """A numerical problem under regression watch.

    solve(seed) -> (solution, iterations)
    error(solution) -> float   # distance to the high-precision reference
    """

    name: str
    kind: str  # "deterministic" | "randomized"

    def solve(self, seed: Optional[int]): ...
    def error(self, solution) -> float: ...


def measure(
    problem: Problem,
    runs: int,
    seed0: int = 0,
) -> Tuple[List[float], List[float], List[float]]:
    """Run ``problem`` ``runs`` times; return (errors, iterations, times_ms)."""
    errors: List[float] = []
    iterations: List[float] = []
    times_ms: List[float] = []
    for i in range(runs):
        seed = None if problem.kind == "deterministic" else seed0 + i
        start = time.perf_counter()
        solution, iters = problem.solve(seed)
        elapsed_ms = (time.perf_counter() - start) * 1e3
        errors.append(problem.error(solution))
        iterations.append(iters)
        times_ms.append(elapsed_ms)
    return errors, iterations, times_ms
