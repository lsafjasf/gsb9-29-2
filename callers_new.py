"""改造后的调用点：四个问题共用同一套 solve()/SolveResult 处理逻辑，
不再有任何按求解器定制的分支。"""

import math

from unified import Problem, solve

SQRT2_TOL = 1e-8
CUBIC_TOL = 1e-10
COS_TOL = 1e-10
EXPDECAY_TOL = 1e-10


def _wrap(result):
    return {"ok": result.ok, "x": result.x, "reason": result.reason.value}


def solve_sqrt2():
    """x^2 - 2 = 0，二分法。"""
    return _wrap(solve(
        Problem(method="bisection", f=lambda x: x * x - 2.0, bracket=(0.0, 2.0)),
        tol=SQRT2_TOL, max_iter=200,
    ))


def solve_cubic():
    """x^3 - x - 2 = 0，牛顿法。"""
    return _wrap(solve(
        Problem(
            method="newton",
            f=lambda x: x ** 3 - x - 2.0,
            df=lambda x: 3.0 * x * x - 1.0,
            x0=1.5,
        ),
        tol=CUBIC_TOL, max_iter=50,
    ))


def solve_cos():
    """cos(x) - x = 0，割线法。"""
    return _wrap(solve(
        Problem(method="secant", f=lambda x: math.cos(x) - x, x0=0.0, x1=1.0),
        tol=COS_TOL, max_iter=100,
    ))


def solve_exp_decay():
    """x = e^{-x}，不动点迭代。"""
    return _wrap(solve(
        Problem(method="fixed_point", g=lambda x: math.exp(-x), x0=0.5),
        tol=EXPDECAY_TOL, max_iter=500,
    ))
