"""统一求解器接口。

统一语义（所有方法一致）：
- tol       : 绝对残差容差，单位与 f(x) 相同（无量纲问题即无量纲）。
              收敛判据为 |f(x)| <= tol；不动点问题的残差定义为 |g(x) - x|。
- max_iter  : 允许执行的“迭代更新步数”（每产生一个新 x 计 1 次，初始点计 0），
              必须为正整数。
- 返回 SolveResult，reason 四选一：
    CONVERGED        收敛（残差达标）
    MAX_ITER_EXCEEDED 超过 max_iter 仍未收敛
    DIVERGED         发散（出现非有限值、零/非有限导数、零割线分母等）
    INVALID_INPUT    输入非法（参数缺/错、区间不夹根等）
  任何路径都不抛异常、不返回 None 歧义值。
"""

import math
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Tuple


class StopReason(Enum):
    CONVERGED = "converged"
    MAX_ITER_EXCEEDED = "max_iter_exceeded"
    DIVERGED = "diverged"
    INVALID_INPUT = "invalid_input"


@dataclass(frozen=True)
class Problem:
    """问题描述。

    method: "bisection" | "newton" | "secant" | "fixed_point"
    f/df  : 根问题函数 f(x)=0 及其导数（newton 需要 df）
    g     : 不动点问题 x = g(x)
    bracket: (a, b) 且 f(a) 与 f(b) 异号（bisection）
    x0/x1 : 初值（secant 需要两个）
    """

    method: str
    f: Optional[Callable[[float], float]] = None
    df: Optional[Callable[[float], float]] = None
    g: Optional[Callable[[float], float]] = None
    bracket: Optional[Tuple[float, float]] = None
    x0: Optional[float] = None
    x1: Optional[float] = None


@dataclass(frozen=True)
class SolveResult:
    x: Optional[float]
    residual: Optional[float]
    reason: StopReason
    iterations: int
    method: str
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.reason is StopReason.CONVERGED


_DIVERGENCE_ABS = 1e150


def _result(x, residual, reason, iterations, method, message=""):
    return SolveResult(
        x=x,
        residual=residual,
        reason=reason,
        iterations=iterations,
        method=method,
        message=message,
    )


def _is_real(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def _validate(problem, tol, max_iter):
    if not isinstance(problem, Problem):
        return "problem must be a Problem instance"
    if problem.method not in ("bisection", "newton", "secant", "fixed_point"):
        return f"unknown method: {problem.method!r}"
    if not _is_real(tol) or tol <= 0:
        return "tol must be a finite positive real number"
    if not isinstance(max_iter, int) or isinstance(max_iter, bool) or max_iter < 1:
        return "max_iter must be a positive integer"

    if problem.method == "bisection":
        if not callable(problem.f):
            return "bisection requires callable f"
        bk = problem.bracket
        if not isinstance(bk, tuple) or len(bk) != 2:
            return "bisection requires bracket (a, b)"
        if not (_is_real(bk[0]) and _is_real(bk[1])):
            return "bracket endpoints must be finite real numbers"
        if bk[0] >= bk[1]:
            return "bracket requires a < b"
    elif problem.method == "newton":
        if not callable(problem.f) or not callable(problem.df):
            return "newton requires callable f and df"
        if not _is_real(problem.x0):
            return "newton requires finite x0"
    elif problem.method == "secant":
        if not callable(problem.f):
            return "secant requires callable f"
        if not (_is_real(problem.x0) and _is_real(problem.x1)):
            return "secant requires finite x0 and x1"
    else:  # fixed_point
        if not callable(problem.g):
            return "fixed_point requires callable g"
        if not _is_real(problem.x0):
            return "fixed_point requires finite x0"
    return None


def solve(problem: Problem, *, tol: float, max_iter: int) -> SolveResult:
    """统一入口。任何错误都以 reason 表达，不抛异常。"""
    invalid = _validate(problem, tol, max_iter)
    if invalid is not None:
        method = problem.method if isinstance(problem, Problem) else "<invalid>"
        return _result(None, None, StopReason.INVALID_INPUT, 0, method, invalid)

    if problem.method == "bisection":
        return _bisection(problem, tol, max_iter)
    if problem.method == "newton":
        return _newton(problem, tol, max_iter)
    if problem.method == "secant":
        return _secant(problem, tol, max_iter)
    return _fixed_point(problem, tol, max_iter)


def _bisection(p, tol, max_iter):
    a, b = p.bracket
    fa, fb = p.f(a), p.f(b)
    if not (math.isfinite(fa) and math.isfinite(fb)):
        return _result(None, None, StopReason.INVALID_INPUT, 0, "bisection",
                       "f is not finite at bracket endpoints")
    if fa == 0:
        return _result(float(a), 0.0, StopReason.CONVERGED, 0, "bisection")
    if fb == 0:
        return _result(float(b), 0.0, StopReason.CONVERGED, 0, "bisection")
    if fa * fb > 0:
        return _result(None, None, StopReason.INVALID_INPUT, 0, "bisection",
                       "f(a) and f(b) must have opposite signs")

    mid, fm = a, fa
    for i in range(1, max_iter + 1):
        mid = (a + b) / 2.0
        fm = p.f(mid)
        if not math.isfinite(fm):
            return _result(mid, None, StopReason.DIVERGED, i, "bisection",
                           "non-finite f value")
        if abs(fm) <= tol:
            return _result(mid, abs(fm), StopReason.CONVERGED, i, "bisection")
        if fa * fm < 0:
            b, fb = mid, fm
        else:
            a, fa = mid, fm
    return _result(mid, abs(fm), StopReason.MAX_ITER_EXCEEDED, max_iter, "bisection",
                   f"residual {abs(fm):.3e} > tol {tol:.3e}")


def _newton(p, tol, max_iter):
    x = float(p.x0)
    updates = 0
    while True:
        fx = p.f(x)
        if not math.isfinite(fx):
            return _result(x, None, StopReason.DIVERGED, updates, "newton",
                           "non-finite f value")
        if abs(fx) <= tol:
            return _result(x, abs(fx), StopReason.CONVERGED, updates, "newton")
        if updates == max_iter:
            return _result(x, abs(fx), StopReason.MAX_ITER_EXCEEDED, updates, "newton",
                           f"residual {abs(fx):.3e} > tol {tol:.3e}")
        dfx = p.df(x)
        if not math.isfinite(dfx) or dfx == 0:
            return _result(x, abs(fx), StopReason.DIVERGED, updates, "newton",
                           "zero or non-finite derivative")
        x = x - fx / dfx
        if not math.isfinite(x) or abs(x) > _DIVERGENCE_ABS:
            return _result(None, None, StopReason.DIVERGED, updates, "newton",
                           "iterate became non-finite or too large")
        updates += 1


def _secant(p, tol, max_iter):
    x0, x1 = float(p.x0), float(p.x1)
    f0, f1 = p.f(x0), p.f(x1)
    if not (math.isfinite(f0) and math.isfinite(f1)):
        return _result(None, None, StopReason.INVALID_INPUT, 0, "secant",
                       "f is not finite at initial points")
    if abs(f0) <= tol:
        return _result(x0, abs(f0), StopReason.CONVERGED, 0, "secant")
    if abs(f1) <= tol:
        return _result(x1, abs(f1), StopReason.CONVERGED, 0, "secant")

    for i in range(1, max_iter + 1):
        denom = f1 - f0
        if denom == 0 or not math.isfinite(denom):
            return _result(x1, abs(f1), StopReason.DIVERGED, i, "secant",
                           "zero or non-finite secant denominator")
        x2 = x1 - f1 * (x1 - x0) / denom
        if not math.isfinite(x2) or abs(x2) > _DIVERGENCE_ABS:
            return _result(None, None, StopReason.DIVERGED, i, "secant",
                           "iterate became non-finite or too large")
        f2 = p.f(x2)
        if not math.isfinite(f2):
            return _result(x2, None, StopReason.DIVERGED, i, "secant",
                           "non-finite f value")
        if abs(f2) <= tol:
            return _result(x2, abs(f2), StopReason.CONVERGED, i, "secant")
        x0, f0, x1, f1 = x1, f1, x2, f2
    return _result(x1, abs(f1), StopReason.MAX_ITER_EXCEEDED, max_iter, "secant",
                   f"residual {abs(f1):.3e} > tol {tol:.3e}")


def _fixed_point(p, tol, max_iter):
    x = float(p.x0)
    updates = 0
    while True:
        try:
            gx = p.g(x)
        except (OverflowError, ZeroDivisionError) as exc:
            return _result(None, None, StopReason.DIVERGED, updates, "fixed_point",
                           f"{type(exc).__name__} while evaluating g")
        if not math.isfinite(gx):
            return _result(None, None, StopReason.DIVERGED, updates, "fixed_point",
                           "non-finite iterate")
        residual = abs(gx - x)
        if residual <= tol:
            return _result(x, residual, StopReason.CONVERGED, updates, "fixed_point")
        if updates == max_iter:
            return _result(x, residual, StopReason.MAX_ITER_EXCEEDED, updates,
                           "fixed_point", f"residual {residual:.3e} > tol {tol:.3e}")
        x = gx
        updates += 1
