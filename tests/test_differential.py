"""差分测试：统一接口 vs 历史求解器，逐调用点对拍。

判定准则：
- 解一致：|x_new - x_legacy| <= 该调用点容差（或同阶放大容忍，见各用例注释）
- 残差一致：两侧残差在同一容差量级内
- 停止原因可按对照表映射（见 docs/PARAMETER_MAPPING.md）
"""

import math
import unittest

import callers_new
from legacy import callers as legacy_callers
from legacy import solvers as legacy
from unified import Problem, StopReason, solve


class TestCallSiteDiff(unittest.TestCase):
    """四个原调用点：新旧路径的解与 ok 标志必须一致。"""

    def test_sqrt2_site(self):
        old = legacy_callers.solve_sqrt2_legacy()
        new = callers_new.solve_sqrt2()
        self.assertTrue(old["ok"] and new["ok"])
        self.assertLessEqual(abs(old["x"] - new["x"]), 2 * callers_new.SQRT2_TOL)
        self.assertLessEqual(abs(new["x"] - math.sqrt(2.0)), callers_new.SQRT2_TOL)

    def test_cubic_site(self):
        old = legacy_callers.solve_cubic_legacy()
        new = callers_new.solve_cubic()
        self.assertTrue(old["ok"] and new["ok"])
        # 牛顿法两侧判据同为 |f(x)|<=tol，迭代序列逐位一致
        self.assertEqual(old["x"], new["x"])

    def test_cos_site(self):
        old = legacy_callers.solve_cos_legacy()
        new = callers_new.solve_cos()
        self.assertTrue(old["ok"] and new["ok"])
        # 旧判据是步长 |dx|<=delta，新判据是残差；f' 有界 => 解差与容差同阶
        self.assertLessEqual(abs(old["x"] - new["x"]), 1e-6)
        self.assertLessEqual(abs(math.cos(new["x"]) - new["x"]), callers_new.COS_TOL)

    def test_exp_decay_site(self):
        old = legacy_callers.solve_exp_decay_legacy()
        new = callers_new.solve_exp_decay()
        self.assertTrue(old["ok"] and new["ok"])
        # 旧实现返回停止时的“新迭代点”，新实现返回残差达标的当前点，相差 <= tol
        self.assertLessEqual(abs(old["x"] - new["x"]), 2 * callers_new.EXPDECAY_TOL)


class TestSolverLevelDiff(unittest.TestCase):
    """求解器级别对拍：成功路径解一致，失败路径停止原因可映射。"""

    def test_bisection_success(self):
        f = lambda x: x * x - 2.0
        x_old, _ = legacy.bisect_solve(f, 0.0, 2.0, tol=1e-8, max_iter=200)
        r = solve(Problem(method="bisection", f=f, bracket=(0.0, 2.0)),
                  tol=1e-8, max_iter=200)
        self.assertEqual(r.reason, StopReason.CONVERGED)
        self.assertLessEqual(abs(x_old - r.x), 1e-8)
        self.assertLessEqual(r.residual, 1e-8)

    def test_newton_success(self):
        f = lambda x: x ** 3 - x - 2.0
        df = lambda x: 3.0 * x * x - 1.0
        x_old = legacy.newton_solve(f, df, 1.5, eps=1e-10, itmax=50)
        r = solve(Problem(method="newton", f=f, df=df, x0=1.5),
                  tol=1e-10, max_iter=50)
        self.assertEqual(r.reason, StopReason.CONVERGED)
        self.assertEqual(x_old, r.x)

    def test_secant_success(self):
        f = lambda x: math.cos(x) - x
        old = legacy.secant_solve(f, 0.0, 1.0, delta=1e-10, nmax=100)
        r = solve(Problem(method="secant", f=f, x0=0.0, x1=1.0),
                  tol=1e-10, max_iter=100)
        self.assertEqual(old["code"], 0)
        self.assertEqual(r.reason, StopReason.CONVERGED)
        self.assertLessEqual(abs(old["root"] - r.x), 1e-6)
        self.assertLessEqual(abs(f(old["root"])), 1e-8)
        self.assertLessEqual(r.residual, 1e-10)

    def test_fixed_point_success(self):
        g = lambda x: math.exp(-x)
        xs = legacy.fixed_point_iter(g, 0.5, tol=1e-10, maxit=500)
        r = solve(Problem(method="fixed_point", g=g, x0=0.5),
                  tol=1e-10, max_iter=500)
        self.assertEqual(r.reason, StopReason.CONVERGED)
        self.assertLessEqual(abs(xs[-1] - r.x), 2e-10)


class TestStopReasonMapping(unittest.TestCase):
    """失败路径：旧的五花八门失败表示 -> 统一 StopReason 的映射。"""

    def test_bisection_bad_bracket_maps_to_invalid_input(self):
        f = lambda x: x * x + 1.0
        with self.assertRaises(ValueError):
            legacy.bisect_solve(f, 0.0, 2.0)
        r = solve(Problem(method="bisection", f=f, bracket=(0.0, 2.0)),
                  tol=1e-6, max_iter=100)
        self.assertEqual(r.reason, StopReason.INVALID_INPUT)

    def test_bisection_max_iter_maps(self):
        # 旧实现超限静默返回且 n == max_iter（调用方无法区分，只能猜）
        f = lambda x: x * x - 2.0
        _, n = legacy.bisect_solve(f, 0.0, 2.0, tol=1e-15, max_iter=3)
        self.assertEqual(n, 3)
        r = solve(Problem(method="bisection", f=f, bracket=(0.0, 2.0)),
                  tol=1e-15, max_iter=3)
        self.assertEqual(r.reason, StopReason.MAX_ITER_EXCEEDED)

    def test_newton_zero_derivative_maps_to_diverged(self):
        f = lambda x: x * x + 1.0
        df = lambda x: 2.0 * x
        self.assertIsNone(legacy.newton_solve(f, df, 0.0))
        r = solve(Problem(method="newton", f=f, df=df, x0=0.0),
                  tol=1e-10, max_iter=50)
        self.assertEqual(r.reason, StopReason.DIVERGED)

    def test_newton_max_iter_maps(self):
        # 旧实现超限同样返回 None（与零导数无法区分）；新实现明确区分
        self.assertIsNone(legacy.newton_solve(math.exp, math.exp, 1.0,
                                              eps=1e-30, itmax=5))
        r = solve(Problem(method="newton", f=math.exp, df=math.exp, x0=1.0),
                  tol=1e-30, max_iter=5)
        self.assertEqual(r.reason, StopReason.MAX_ITER_EXCEEDED)

    def test_secant_zero_denominator_maps_to_diverged(self):
        f = lambda x: x * x + 1.0
        old = legacy.secant_solve(f, -1.0, 1.0)
        self.assertEqual(old["code"], 2)
        r = solve(Problem(method="secant", f=f, x0=-1.0, x1=1.0),
                  tol=1e-10, max_iter=50)
        self.assertEqual(r.reason, StopReason.DIVERGED)

    def test_secant_max_iter_maps(self):
        f = lambda x: math.cos(x) - x
        old = legacy.secant_solve(f, 0.0, 1.0, delta=1e-18, nmax=2)
        self.assertEqual(old["code"], 1)
        r = solve(Problem(method="secant", f=f, x0=0.0, x1=1.0),
                  tol=1e-18, max_iter=2)
        self.assertEqual(r.reason, StopReason.MAX_ITER_EXCEEDED)

    def test_fixed_point_divergence_maps(self):
        g = lambda x: x * x
        xs = legacy.fixed_point_iter(g, 2.0, tol=1e-10, maxit=100)
        self.assertTrue(any(math.isinf(v) for v in xs))
        r = solve(Problem(method="fixed_point", g=g, x0=2.0),
                  tol=1e-10, max_iter=100)
        self.assertEqual(r.reason, StopReason.DIVERGED)

    def test_fixed_point_max_iter_maps(self):
        xs = legacy.fixed_point_iter(math.cos, 1.0, tol=1e-18, maxit=5)
        self.assertGreater(abs(xs[-1] - xs[-2]), 1e-18)
        r = solve(Problem(method="fixed_point", g=math.cos, x0=1.0),
                  tol=1e-18, max_iter=5)
        self.assertEqual(r.reason, StopReason.MAX_ITER_EXCEEDED)


if __name__ == "__main__":
    unittest.main()
