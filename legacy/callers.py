"""重构前的调用点：每个都要写一堆分支去适配各自求解器的返回约定。"""

import math

from legacy.solvers import (
    bisect_solve,
    fixed_point_iter,
    newton_solve,
    secant_solve,
)

SQRT2_TOL = 1e-8
CUBIC_TOL = 1e-10
COS_TOL = 1e-10
EXPDECAY_TOL = 1e-10


def solve_sqrt2_legacy():
    """x^2 - 2 = 0，二分法。要 try/except 兜 ValueError，且无法识别超限。"""
    try:
        root, _n = bisect_solve(
            lambda x: x * x - 2.0, 0.0, 2.0, tol=SQRT2_TOL, max_iter=200
        )
        return {"ok": True, "x": root}
    except ValueError:
        return {"ok": False, "x": None}


def solve_cubic_legacy():
    """x^3 - x - 2 = 0，牛顿法。None 同时表示零导数和超限，只能笼统判失败。"""
    r = newton_solve(
        lambda x: x ** 3 - x - 2.0,
        lambda x: 3.0 * x * x - 1.0,
        1.5,
        eps=CUBIC_TOL,
        itmax=50,
    )
    if r is None:
        return {"ok": False, "x": None}
    return {"ok": True, "x": r}


def solve_cos_legacy():
    """cos(x) - x = 0，割线法。要解读自定义 code。"""
    res = secant_solve(lambda x: math.cos(x) - x, 0.0, 1.0, delta=COS_TOL, nmax=100)
    if res["code"] == 0:
        return {"ok": True, "x": res["root"]}
    return {"ok": False, "x": None}


def solve_exp_decay_legacy():
    """x = e^{-x}，不动点迭代。要自己翻列表判 nan/inf 和末两项间距。"""
    xs = fixed_point_iter(lambda x: math.exp(-x), 0.5, tol=EXPDECAY_TOL, maxit=500)
    last = xs[-1]
    if isinstance(last, float) and (math.isnan(last) or math.isinf(last)):
        return {"ok": False, "x": None}
    if len(xs) >= 2 and abs(xs[-1] - xs[-2]) <= EXPDECAY_TOL:
        return {"ok": True, "x": last}
    return {"ok": False, "x": None}
