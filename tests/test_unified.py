"""Regression tests for the unified solver interface.

Covers every StopReason for every engine: CONVERGED, MAX_ITER, DIVERGED,
INVALID_INPUT, plus auto method selection and the RootProblem contract.
"""

import math
import unittest

from solver import RootProblem, StopReason, solve


def cbrt(x):
    return math.copysign(abs(x) ** (1.0 / 3.0), x)


def cbrt_p(x):
    return (1.0 / 3.0) * abs(x) ** (-2.0 / 3.0)


class BisectionTests(unittest.TestCase):
    def test_converged(self):
        result = solve(
            RootProblem(f=lambda x: x - math.cos(x), bracket=(0.0, 2.0)),
            tol=1e-10,
            method="bisection",
        )
        self.assertIs(result.reason, StopReason.CONVERGED)
        self.assertLessEqual(result.residual, 1e-10)
        self.assertAlmostEqual(result.x, 0.7390851332151607, places=8)
        self.assertGreaterEqual(result.iterations, 1)

    def test_converged_at_endpoint(self):
        result = solve(
            RootProblem(f=lambda x: x, bracket=(0.0, 1.0)), method="bisection"
        )
        self.assertIs(result.reason, StopReason.CONVERGED)
        self.assertEqual(result.x, 0.0)
        self.assertEqual(result.residual, 0.0)

    def test_max_iter(self):
        result = solve(
            RootProblem(f=lambda x: math.sin(x) - 0.5, bracket=(0.0, 1.0)),
            tol=1e-15,
            max_iter=3,
            method="bisection",
        )
        self.assertIs(result.reason, StopReason.MAX_ITER)
        self.assertEqual(result.iterations, 3)
        self.assertGreater(result.residual, 1e-15)

    def test_diverged_mid_iteration_domain_error(self):
        def f(x):
            if 0.49 <= x <= 0.51:
                raise ValueError("domain blowup")
            return x - 0.2

        result = solve(
            RootProblem(f=f, bracket=(0.0, 2.0)),
            tol=1e-12,
            method="bisection",
        )
        self.assertIs(result.reason, StopReason.DIVERGED)
        self.assertTrue(math.isnan(result.residual))
        self.assertIn("mid-iteration", result.message)

    def test_diverged_nan_midpoint(self):
        result = solve(
            RootProblem(f=lambda x: float("nan"), bracket=(0.0, 1.0)),
            method="bisection",
        )
        self.assertIs(result.reason, StopReason.DIVERGED)

    def test_invalid_bracket(self):
        for kwargs, problem in [
            (dict(method="bisection"), RootProblem(f=lambda x: x)),
            (dict(method="bisection"),
             RootProblem(f=lambda x: x, bracket=(1.0, 0.0))),
            (dict(method="bisection"),
             RootProblem(f=lambda x: x + 1, bracket=(0.0, 1.0))),
            (dict(method="bisection"),
             RootProblem(f=lambda x: x, bracket=(0.0, float("inf")))),
        ]:
            with self.subTest(problem=problem):
                result = solve(problem, **kwargs)
                self.assertIs(result.reason, StopReason.INVALID_INPUT)

    def test_invalid_domain_error_at_endpoint(self):
        result = solve(
            RootProblem(f=lambda x: math.sqrt(x - 1.0), bracket=(0.0, 2.0)),
            method="bisection",
        )
        self.assertIs(result.reason, StopReason.INVALID_INPUT)


class NewtonTests(unittest.TestCase):
    def test_converged(self):
        result = solve(
            RootProblem(f=lambda x: x * x - 2, fp=lambda x: 2 * x, x0=1.0),
            tol=1e-12,
            max_iter=50,
            method="newton",
        )
        self.assertIs(result.reason, StopReason.CONVERGED)
        self.assertEqual(result.x, 1.4142135623730951)
        self.assertLessEqual(result.residual, 1e-12)

    def test_converged_at_x0_counts_zero_iterations(self):
        result = solve(
            RootProblem(f=lambda x: x - 1.0, fp=lambda x: 1.0, x0=1.0),
            method="newton",
        )
        self.assertIs(result.reason, StopReason.CONVERGED)
        self.assertEqual(result.iterations, 0)

    def test_max_iter_non_converging_cycle(self):
        result = solve(
            RootProblem(
                f=lambda x: x ** 3 - 2 * x + 2,
                fp=lambda x: 3 * x * x - 2,
                x0=0.0,
            ),
            tol=1e-12,
            max_iter=4,
            method="newton",
        )
        self.assertIs(result.reason, StopReason.MAX_ITER)
        self.assertEqual(result.iterations, 4)
        self.assertEqual(result.x, 0.0)
        self.assertEqual(result.residual, 2.0)

    def test_diverged_blowup(self):
        result = solve(
            RootProblem(f=cbrt, fp=cbrt_p, x0=1.0),
            tol=1e-12,
            method="newton",
        )
        self.assertIs(result.reason, StopReason.DIVERGED)
        self.assertGreater(abs(result.x), 1e12)
        self.assertTrue(math.isnan(result.residual))

    def test_diverged_zero_derivative(self):
        result = solve(
            RootProblem(f=lambda x: 1.0, fp=lambda x: 0.0, x0=1.0),
            method="newton",
        )
        self.assertIs(result.reason, StopReason.DIVERGED)
        self.assertIn("derivative evaluated to zero", result.message)

    def test_diverged_nan_residual(self):
        result = solve(
            RootProblem(f=lambda x: float("nan"), fp=lambda x: 1.0, x0=1.0),
            method="newton",
        )
        self.assertIs(result.reason, StopReason.DIVERGED)

    def test_invalid_input(self):
        cases = [
            RootProblem(f=lambda x: x, fp=lambda x: 1.0),
            RootProblem(f=lambda x: x, x0=1.0),
            RootProblem(f=lambda x: x, fp=lambda x: 1.0, x0=float("nan")),
        ]
        for problem in cases:
            with self.subTest(problem=problem):
                self.assertIs(
                    solve(problem, method="newton").reason,
                    StopReason.INVALID_INPUT,
                )


class FixedPointTests(unittest.TestCase):
    def test_converged(self):
        result = solve(
            RootProblem(g=math.cos, x0=0.5),
            tol=1e-7,
            method="fixedpoint",
        )
        self.assertIs(result.reason, StopReason.CONVERGED)
        self.assertAlmostEqual(result.x, math.cos(result.x), places=6)
        self.assertEqual(result.x, 0.739085104225471)
        self.assertEqual(result.residual, 4.8517492912125704e-08)

    def test_max_iter(self):
        result = solve(
            RootProblem(g=lambda x: x + 1.0, x0=0.0),
            tol=1e-9,
            max_iter=5,
            method="fixedpoint",
        )
        self.assertIs(result.reason, StopReason.MAX_ITER)
        self.assertEqual(result.x, 5.0)
        self.assertEqual(result.residual, 1.0)
        self.assertEqual(result.iterations, 5)

    def test_diverged(self):
        result = solve(
            RootProblem(g=lambda x: 2.0 * x, x0=1.0),
            tol=1e-9,
            method="fixedpoint",
        )
        self.assertIs(result.reason, StopReason.DIVERGED)
        self.assertEqual(result.x, 1099511627776.0)
        self.assertTrue(math.isnan(result.residual))

    def test_invalid_input(self):
        cases = [
            RootProblem(g=math.cos),
            RootProblem(x0=0.5),
            RootProblem(g=math.cos, x0=float("inf")),
        ]
        for problem in cases:
            with self.subTest(problem=problem):
                self.assertIs(
                    solve(problem, method="fixedpoint").reason,
                    StopReason.INVALID_INPUT,
                )


class ContractTests(unittest.TestCase):
    def test_bad_controls(self):
        problem = RootProblem(f=lambda x: x, fp=lambda x: 1.0, x0=1.0)
        for tol, max_iter in [
            (0.0, 10), (1e-8, 0), (1e-8, 1.5), (-1e-8, 10),
            (float("inf"), 10),
        ]:
            with self.subTest(tol=tol, max_iter=max_iter):
                self.assertIs(
                    solve(problem, tol=tol, max_iter=max_iter).reason,
                    StopReason.INVALID_INPUT,
                )

    def test_bool_rejected_as_number(self):
        problem = RootProblem(f=lambda x: x, bracket=(False, 1.0))
        self.assertIs(
            solve(problem, method="bisection").reason, StopReason.INVALID_INPUT
        )

    def test_unknown_method(self):
        problem = RootProblem(f=lambda x: x)
        self.assertIs(
            solve(problem, method="seidel").reason, StopReason.INVALID_INPUT
        )

    def test_auto_selects_engine(self):
        result = solve(
            RootProblem(f=lambda x: x * x - 2, fp=lambda x: 2 * x, x0=1.0),
            tol=1e-10,
        )
        self.assertIs(result.reason, StopReason.CONVERGED)

        result = solve(RootProblem(f=lambda x: x, bracket=(-1.0, 2.0)))
        self.assertIs(result.reason, StopReason.CONVERGED)

        result = solve(RootProblem(g=math.cos, x0=0.5), tol=1e-7)
        self.assertIs(result.reason, StopReason.CONVERGED)

    def test_g_supplies_residual(self):
        result = solve(RootProblem(g=lambda x: 0.5 * (x + 2.0 / x), x0=1.0))
        self.assertIs(result.reason, StopReason.CONVERGED)
        self.assertAlmostEqual(result.x, math.sqrt(2.0), places=8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
