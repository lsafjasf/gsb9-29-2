"""Differential tests: refactored call sites vs. the legacy implementations.

Equivalence rules per scenario:

* CONVERGED  - x and |residual| are bit-identical for Newton and fixed-point.
               Bisection's legacy tol is an x-space bracket width while the
               unified tol is a residual, so the two stop one iteration apart;
               instead we require |x_new - x_old| <= legacy_width_tol and both
               residuals bounded by the mapped residual tolerance (see
               docs/MAPPING.md).
* MAX_ITER   - reason category must match; Newton's legacy returns None (no
               state), fixed-point keeps the previous-step residual while the
               unified contract always evaluates the residual at x; at call
               sites these cases are compared by category (and bit-for-bit
               where the legacy path retained the value, e.g. constant-step
               maps and bisection midpoints).
* DIVERGED   - reason category must match (legacy exceptions/status codes map
               to StopReason.DIVERGED); x is bit-identical at the shared
               blow-up guard for fixed-point.
* INVALID    - reason category and the caller-visible x/residual payload must
               match.
"""

import math
import unittest

from app import equilibrium as new_eq_mod
from app import tank_level as new_tank
from app import yield_curve as new_yield
from legacy import equilibrium as old_eq_mod
from legacy import tank_level as old_tank
from legacy import yield_curve as old_yield
from legacy.bisect_solver import bisect as legacy_bisect
from legacy.fixedpoint_solver import FixedPointSolver
from legacy.newton_solver import newton as legacy_newton
from solver import RootProblem, StopReason, solve

CASH_FLOWS = [-100.0, 10.0, 10.0, 110.0]


def same_optional(a, b):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, float) and math.isnan(a):
        return isinstance(b, float) and math.isnan(b)
    return a == b


class TankCallSiteTests(unittest.TestCase):
    """Bisection call site: width-tol (legacy) vs residual-tol (unified)."""

    def test_converged_equivalent_within_tolerance_band(self):
        old = old_tank.find_level(0.5)
        new = new_tank.find_level(0.5, tol=3e-6)
        self.assertEqual(old[0], "ok")
        self.assertEqual(new[0], "ok")
        self.assertLessEqual(abs(new[1] - old[1]), 1e-6)
        self.assertLessEqual(old[2], 1e-6 * 3.0 + 1e-15)
        self.assertLessEqual(new[2], 3e-6)

    def test_converged_exact_when_root_is_a_midpoint(self):
        target = math.pi  # half-full cylinder => root exactly at h = r = 1
        old = old_tank.find_level(target)
        new = new_tank.find_level(target)
        self.assertEqual((old[0], old[1], old[2]), (new[0], new[1], new[2]))

    def test_max_iter_reason_and_midpoint_match(self):
        old = old_tank.find_level(0.5, maxit=3)
        new = new_tank.find_level(0.5, max_iter=3)
        self.assertEqual(old[0], "no-convergence")
        self.assertEqual(new[0], "no-convergence")
        self.assertEqual(old[1], new[1])
        self.assertEqual(abs(old[2]), new[2])

    def test_invalid_input(self):
        for target in (10.0, -1.0):
            with self.subTest(target=target):
                old = old_tank.find_level(target)
                new = new_tank.find_level(target)
                self.assertEqual(old, ("bad-input", None, None))
                self.assertEqual(new, ("bad-input", None, None))


class YieldCallSiteTests(unittest.TestCase):
    """Newton call site: dict/None/exceptions (legacy) vs result enum."""

    def test_converged_bit_identical(self):
        for guess in (0.1, -0.9, 0.25):
            with self.subTest(guess=guess):
                old = old_yield.find_yield(0.0, CASH_FLOWS, guess=guess)
                new = new_yield.find_yield(0.0, CASH_FLOWS, guess=guess)
                self.assertEqual(old[0], new[0])
                self.assertEqual(old[1], new[1])
                self.assertEqual(old[2], new[2])

    def test_diverged_blowup_reason_matches(self):
        for guess in (-2.0, 5.0):
            with self.subTest(guess=guess):
                old = old_yield.find_yield(0.0, CASH_FLOWS, guess=guess)
                new = new_yield.find_yield(0.0, CASH_FLOWS, guess=guess)
                self.assertEqual(old[0], "blew-up")
                self.assertEqual(new[0], "blew-up")

    def test_stalled_zero_derivative_reason_matches(self):
        from legacy.newton_solver import newton as raw_newton

        with self.assertRaises(ZeroDivisionError):
            raw_newton(lambda x: 1.0, lambda x: 0.0, 1.0)
        result = solve(
            RootProblem(f=lambda x: 1.0, fp=lambda x: 0.0, x0=1.0),
            method="newton",
        )
        self.assertIs(result.reason, StopReason.DIVERGED)

    def test_invalid_input(self):
        old = old_yield.find_yield(0.0, CASH_FLOWS, guess=float("nan"))
        new = new_yield.find_yield(0.0, CASH_FLOWS, guess=float("nan"))
        self.assertEqual(old[0], "bad-guess")
        self.assertEqual(new[0], "bad-guess")
        self.assertTrue(math.isnan(old[1]) and math.isnan(new[1]))


class EquilibriumCallSiteTests(unittest.TestCase):
    """Fixed-point call site: integer codes (legacy) vs result enum."""

    def test_converged_bit_identical(self):
        old = old_eq_mod.find_equilibrium()
        new = new_eq_mod.find_equilibrium()
        self.assertEqual(old, new)
        self.assertEqual((old[0], old[1], old[2]), ("ok", 0.739085104225471, 4.8517492912125704e-08))

    def test_max_iter_bit_identical_for_constant_step(self):
        old = old_eq_mod.find_equilibrium(lambda x: x + 1.0, x0=0.0, maxit=5)
        new = new_eq_mod.find_equilibrium(lambda x: x + 1.0, x0=0.0, max_iter=5)
        self.assertEqual(old, new)

    def test_max_iter_reason_matches_generic_map(self):
        old = old_eq_mod.find_equilibrium(lambda x: 0.5 * x + 1.0, x0=0.0, maxit=3)
        new = new_eq_mod.find_equilibrium(lambda x: 0.5 * x + 1.0, x0=0.0, max_iter=3)
        self.assertEqual(old[0], "limit")
        self.assertEqual(new[0], "limit")
        self.assertEqual(old[1], new[1])
        # legacy limit residual is the last |y-x|; unified limit residual is
        # evaluated at x (one g-evaluation later) - documented one-step offset.
        self.assertLessEqual(abs(old[2] - new[2]), old[2])

    def test_diverged_reason_and_point_match(self):
        old = old_eq_mod.find_equilibrium(lambda x: 2.0 * x, x0=1.0)
        new = new_eq_mod.find_equilibrium(lambda x: 2.0 * x, x0=1.0)
        self.assertEqual(old[0], "diverged")
        self.assertEqual(new[0], "diverged")
        self.assertEqual(old[1], new[1])

    def test_invalid_input(self):
        old = old_eq_mod.find_equilibrium(x0=float("inf"))
        new = new_eq_mod.find_equilibrium(x0=float("inf"))
        self.assertEqual(old[0], "invalid-start")
        self.assertEqual(new[0], "invalid-start")
        self.assertTrue(math.isnan(old[2]) and math.isnan(new[2]))


class EngineLevelReasonMappingTests(unittest.TestCase):
    """Reason-category correspondence for outcomes the apps cannot provoke."""

    def test_newton_limit_cycle_legacy_none_maps_to_max_iter(self):
        f = lambda x: x ** 3 - 2 * x + 2
        fp = lambda x: 3 * x * x - 2
        self.assertIsNone(legacy_newton(f, fp, 0.0, max_iter=4, eps=1e-12))
        result = solve(
            RootProblem(f=f, fp=fp, x0=0.0),
            tol=1e-12,
            max_iter=4,
            method="newton",
        )
        self.assertIs(result.reason, StopReason.MAX_ITER)

    def test_newton_blowup_legacy_exception_maps_to_diverged(self):
        cbrt = lambda x: math.copysign(abs(x) ** (1.0 / 3.0), x)
        cbrt_p = lambda x: (1.0 / 3.0) * abs(x) ** (-2.0 / 3.0)
        with self.assertRaises(FloatingPointError):
            legacy_newton(cbrt, cbrt_p, 1.0)
        result = solve(
            RootProblem(f=cbrt, fp=cbrt_p, x0=1.0), tol=1e-12, method="newton"
        )
        self.assertIs(result.reason, StopReason.DIVERGED)

    def test_bisection_mid_iteration_error_legacy_valueerror_maps_to_diverged(self):
        def f(x):
            if 0.49 <= x <= 0.51:
                raise ValueError("domain blowup")
            return x - 0.2

        with self.assertRaises(ValueError):
            legacy_bisect(f, 0.0, 2.0, tol=1e-12)
        result = solve(
            RootProblem(f=f, bracket=(0.0, 2.0)), tol=1e-12, method="bisection"
        )
        self.assertIs(result.reason, StopReason.DIVERGED)

    def test_fixedpoint_status_codes_map_to_enum(self):
        code, x, _ = FixedPointSolver(lambda x: x + 1.0).solve(0.0, maxit=1)
        result = solve(
            RootProblem(g=lambda x: x + 1.0, x0=0.0),
            max_iter=1,
            method="fixedpoint",
        )
        self.assertEqual(code, FixedPointSolver.CODE_LIMIT)
        self.assertIs(result.reason, StopReason.MAX_ITER)
        self.assertEqual(x, result.x)


if __name__ == "__main__":
    unittest.main(verbosity=2)
