"""统一接口回归测试：收敛 / 超限 / 发散 / 输入非法 四类停止原因。"""

import math
import unittest

from unified import Problem, StopReason, solve


class TestConverged(unittest.TestCase):
    def test_bisection(self):
        r = solve(Problem(method="bisection", f=lambda x: x * x - 2.0,
                          bracket=(0.0, 2.0)), tol=1e-12, max_iter=200)
        self.assertEqual(r.reason, StopReason.CONVERGED)
        self.assertAlmostEqual(r.x, math.sqrt(2.0), places=10)
        self.assertLessEqual(r.residual, 1e-12)

    def test_newton(self):
        r = solve(Problem(method="newton", f=lambda x: x ** 3 - x - 2.0,
                          df=lambda x: 3.0 * x * x - 1.0, x0=1.5),
                  tol=1e-12, max_iter=50)
        self.assertEqual(r.reason, StopReason.CONVERGED)
        self.assertAlmostEqual(r.x, 1.5213797068045676, places=10)
        self.assertLessEqual(r.residual, 1e-12)

    def test_secant(self):
        r = solve(Problem(method="secant", f=lambda x: math.cos(x) - x,
                          x0=0.0, x1=1.0), tol=1e-12, max_iter=100)
        self.assertEqual(r.reason, StopReason.CONVERGED)
        self.assertAlmostEqual(r.x, 0.7390851332151607, places=10)
        self.assertLessEqual(r.residual, 1e-12)

    def test_fixed_point(self):
        r = solve(Problem(method="fixed_point", g=lambda x: math.exp(-x), x0=0.5),
                  tol=1e-12, max_iter=500)
        self.assertEqual(r.reason, StopReason.CONVERGED)
        self.assertAlmostEqual(r.x, 0.5671432904097839, places=10)
        self.assertLessEqual(r.residual, 1e-12)

    def test_initial_point_already_converged(self):
        r = solve(Problem(method="newton", f=lambda x: x - 3.0,
                          df=lambda x: 1.0, x0=3.0), tol=1e-8, max_iter=10)
        self.assertEqual(r.reason, StopReason.CONVERGED)
        self.assertEqual(r.iterations, 0)


class TestMaxIterExceeded(unittest.TestCase):
    def test_bisection(self):
        r = solve(Problem(method="bisection", f=lambda x: x * x - 2.0,
                          bracket=(0.0, 2.0)), tol=1e-15, max_iter=3)
        self.assertEqual(r.reason, StopReason.MAX_ITER_EXCEEDED)
        self.assertEqual(r.iterations, 3)
        self.assertIsNotNone(r.x)
        self.assertGreater(r.residual, 1e-15)

    def test_newton_slow_convergence(self):
        # e^x 无根，牛顿法每步 x -= 1，5 步内残差不可能 <= 1e-30
        r = solve(Problem(method="newton", f=math.exp, df=math.exp, x0=1.0),
                  tol=1e-30, max_iter=5)
        self.assertEqual(r.reason, StopReason.MAX_ITER_EXCEEDED)
        self.assertEqual(r.iterations, 5)

    def test_secant(self):
        r = solve(Problem(method="secant", f=lambda x: math.cos(x) - x,
                          x0=0.0, x1=1.0), tol=1e-18, max_iter=2)
        self.assertEqual(r.reason, StopReason.MAX_ITER_EXCEEDED)

    def test_fixed_point(self):
        r = solve(Problem(method="fixed_point", g=math.cos, x0=1.0),
                  tol=1e-18, max_iter=5)
        self.assertEqual(r.reason, StopReason.MAX_ITER_EXCEEDED)
        self.assertEqual(r.iterations, 5)


class TestDiverged(unittest.TestCase):
    def test_newton_zero_derivative(self):
        r = solve(Problem(method="newton", f=lambda x: x * x + 1.0,
                          df=lambda x: 2.0 * x, x0=0.0),
                  tol=1e-10, max_iter=50)
        self.assertEqual(r.reason, StopReason.DIVERGED)

    def test_secant_zero_denominator(self):
        r = solve(Problem(method="secant", f=lambda x: x * x + 1.0,
                          x0=-1.0, x1=1.0), tol=1e-10, max_iter=50)
        self.assertEqual(r.reason, StopReason.DIVERGED)

    def test_fixed_point_overflow(self):
        r = solve(Problem(method="fixed_point", g=lambda x: x * x, x0=2.0),
                  tol=1e-10, max_iter=100)
        self.assertEqual(r.reason, StopReason.DIVERGED)

    def test_bisection_nonfinite_f(self):
        r = solve(Problem(method="bisection",
                          f=lambda x: x if abs(x) > 0.1 else float("nan"),
                          bracket=(-1.0, 1.0)), tol=1e-10, max_iter=100)
        self.assertEqual(r.reason, StopReason.DIVERGED)


class TestInvalidInput(unittest.TestCase):
    def _bad(self, **kw):
        problem = kw.pop("problem", Problem(method="newton", f=lambda x: x,
                                            df=lambda x: 1.0, x0=0.0))
        return solve(problem, **kw)

    def test_bad_tol(self):
        for tol in (0.0, -1e-3, float("nan"), float("inf"), "1e-3"):
            self.assertEqual(self._bad(tol=tol, max_iter=10).reason,
                             StopReason.INVALID_INPUT, msg=f"tol={tol!r}")

    def test_bad_max_iter(self):
        for mi in (0, -5, 2.5, "10", True):
            self.assertEqual(self._bad(tol=1e-6, max_iter=mi).reason,
                             StopReason.INVALID_INPUT, msg=f"max_iter={mi!r}")

    def test_unknown_method(self):
        r = solve(Problem(method="rk4", f=lambda x: x), tol=1e-6, max_iter=10)
        self.assertEqual(r.reason, StopReason.INVALID_INPUT)

    def test_bisection_bracket_no_sign_change(self):
        r = solve(Problem(method="bisection", f=lambda x: x * x + 1.0,
                          bracket=(0.0, 2.0)), tol=1e-6, max_iter=100)
        self.assertEqual(r.reason, StopReason.INVALID_INPUT)

    def test_bisection_reversed_bracket(self):
        r = solve(Problem(method="bisection", f=lambda x: x,
                          bracket=(2.0, -2.0)), tol=1e-6, max_iter=100)
        self.assertEqual(r.reason, StopReason.INVALID_INPUT)

    def test_missing_fields(self):
        cases = [
            Problem(method="bisection", bracket=(0.0, 1.0)),          # 缺 f
            Problem(method="bisection", f=lambda x: x),               # 缺 bracket
            Problem(method="newton", f=lambda x: x, x0=1.0),          # 缺 df
            Problem(method="newton", f=lambda x: x, df=lambda x: 1),  # 缺 x0
            Problem(method="secant", f=lambda x: x, x0=0.0),          # 缺 x1
            Problem(method="fixed_point", x0=0.5),                    # 缺 g
        ]
        for p in cases:
            self.assertEqual(solve(p, tol=1e-6, max_iter=10).reason,
                             StopReason.INVALID_INPUT, msg=p)

    def test_result_never_raises(self):
        r = solve("not a problem", tol=1e-6, max_iter=10)
        self.assertEqual(r.reason, StopReason.INVALID_INPUT)
        self.assertFalse(r.ok)


if __name__ == "__main__":
    unittest.main()
