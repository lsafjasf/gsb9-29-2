"""Example numerical problems with high-precision reference solutions.

Three representative cases:

* Sqrt2Newton  -- deterministic, float Newton iteration vs Decimal sqrt.
* JacobiSolver -- deterministic iterative linear solver; iteration count
                  matters (accuracy can be bought with more sweeps).
* MonteCarloPi -- randomized quadrature; error fluctuates run to run.

All references use either :mod:`decimal` at 60 digits or a solution that
is exact by construction (manufactured linear system).
"""

from __future__ import annotations

import math
import random
from decimal import Decimal, localcontext
from typing import List, Optional, Sequence, Tuple


def pi_decimal(prec: int = 60) -> Decimal:
    """Pi to ``prec`` digits via Machin's formula (pure Decimal)."""
    with localcontext() as ctx:
        ctx.prec = prec

        def arctan_inv(x: int) -> Decimal:
            # arctan(1/x) = 1/x - 1/(3x^3) + 1/(5x^5) - ...
            xd = Decimal(x)
            y = Decimal(1) / xd
            y2 = y * y
            term = y
            total = term
            k = 1
            sign = -1
            while term:
                term *= y2
                add = term / Decimal(2 * k + 1)
                if not add:
                    break
                total += sign * add
                sign = -sign
                k += 1
            return +total

        return Decimal(4) * (Decimal(4) * arctan_inv(5) - arctan_inv(239))


class Sqrt2Newton:
    """Newton iteration x <- (x + 2/x)/2, fixed 6 sweeps."""

    name = "sqrt2_newton"
    kind = "deterministic"

    def __init__(self, sweeps: int = 6) -> None:
        self.sweeps = sweeps
        with localcontext() as ctx:
            ctx.prec = 60
            self._ref = Decimal(2).sqrt()

    def solve(self, seed: Optional[int] = None) -> Tuple[float, int]:
        x = 1.0
        iters = 0
        for _ in range(self.sweeps):
            x = 0.5 * (x + 2.0 / x)
            iters += 1
        return x, iters

    def error(self, solution: float) -> float:
        value = Decimal(solution)
        return float(abs((value - self._ref) / self._ref))


class JacobiSolver:
    """Jacobi iteration on a strictly diagonally dominant 6x6 system.

    The right-hand side is manufactured from an exactly-known ``x_true``,
    so the reference solution is exact by construction.
    """

    name = "jacobi_solver"
    kind = "deterministic"
    n = 6

    def __init__(self, tol: float = 1e-12, max_iter: int = 2000) -> None:
        self.tol = tol
        self.max_iter = max_iter
        rng = random.Random(42)
        n = self.n
        a = [[0.0] * n for _ in range(n)]
        for i in range(n):
            for j in range(n):
                if i != j:
                    a[i][j] = rng.uniform(-0.5, 0.5)
            a[i][i] = sum(abs(v) for v in a[i]) + 2.0 + 0.1 * i
        self.a = a
        self.x_true = [(-1.0) ** i * (i + 1) / n for i in range(n)]
        self.b = [sum(a[i][j] * self.x_true[j] for j in range(n))
                  for i in range(n)]

    def solve(self, seed: Optional[int] = None) -> Tuple[List[float], int]:
        n = self.n
        x = [0.0] * n
        iters = 0
        for iters in range(1, self.max_iter + 1):
            x_new = [0.0] * n
            for i in range(n):
                off = sum(self.a[i][j] * x[j] for j in range(n) if j != i)
                x_new[i] = (self.b[i] - off) / self.a[i][i]
            shift = max(abs(x_new[i] - x[i]) for i in range(n))
            x = x_new
            if shift < self.tol:
                break
        return x, iters

    def error(self, solution: Sequence[float]) -> float:
        return max(abs(solution[i] - self.x_true[i]) for i in range(self.n))


class MonteCarloPi:
    """Monte Carlo estimate of pi/4 = integral_0^1 1/(1+x^2) dx."""

    name = "montecarlo_pi"
    kind = "randomized"

    def __init__(self, samples: int = 20000) -> None:
        self.samples = samples
        self._ref = pi_decimal() / Decimal(4)

    def solve(self, seed: Optional[int] = None) -> Tuple[float, int]:
        if seed is None:
            seed = 0
        rng = random.Random(seed)
        n = self.samples
        acc = 0.0
        for _ in range(n):
            x = rng.random()
            acc += 1.0 / (1.0 + x * x)
        return acc / n, n

    def error(self, solution: float) -> float:
        ref = float(self._ref)
        return abs(ref - solution) / ref


class DegradedProblem:
    """Test double: wraps a problem, perturbs its solution and inflates the
    iteration count -- used to prove the checks actually catch a regression
    (and to catch "accuracy bought with more iterations")."""

    def __init__(self, inner, perturb: float = 1e-7,
                 iter_factor: float = 1.6) -> None:
        self._inner = inner
        self.name = inner.name
        self.kind = inner.kind
        self.perturb = perturb
        self.iter_factor = iter_factor

    def solve(self, seed: Optional[int] = None):
        solution, iters = self._inner.solve(seed)
        if isinstance(solution, list):
            solution = [v + self.perturb for v in solution]
        else:
            solution = solution + self.perturb
        return solution, math.ceil(iters * self.iter_factor) + 3

    def error(self, solution) -> float:
        return self._inner.error(solution)


class DegradedProblem:
    """Test double: perturbs the solution and inflates the iteration count.

    Simulates a "new version" whose accuracy silently degrades (or whose
    accuracy is bought with extra iterations).
    """

    def __init__(self, inner, perturb: float = 1e-7, iter_factor: float = 1.6):
        self._inner = inner
        self.name = inner.name
        self.kind = inner.kind
        self.perturb = perturb
        self.iter_factor = iter_factor

    def solve(self, seed: Optional[int] = None):
        solution, iters = self._inner.solve(seed)
        if isinstance(solution, list):
            solution = [v + self.perturb for v in solution]
        else:
            solution = solution + self.perturb
        return solution, math.ceil(iters * self.iter_factor) + 3

    def error(self, solution) -> float:
        return self._inner.error(solution)
