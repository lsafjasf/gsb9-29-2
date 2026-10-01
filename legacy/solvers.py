"""重构前的历史求解器（保持原行为，勿修改——差分测试以其为基准）。

四个求解器各自为政：
- bisect_solve:     返回 (root, n_iter)，非法区间抛 ValueError，超限不报警（静默返回）
- newton_solve:     收敛返回 float，失败（零导数或超限）一律返回 None，无法区分
- secant_solve:     返回 dict + 自定义 code（0/1/2），迭代从 1 起计
- fixed_point_iter: 返回全部迭代点列表，发散/收敛全靠调用方自己看列表
"""

import math


def bisect_solve(f, a, b, tol=1e-6, max_iter=100):
    """二分法。tol 语义：区间半宽 (b-a)/2 <= tol（单位是 x）。

    返回 (root, n_iter)。无根区间抛 ValueError。
    达到 max_iter 时静默返回当前中点，调用方无法区分是否收敛。
    """
    fa, fb = f(a), f(b)
    if fa == 0:
        return a, 0
    if fb == 0:
        return b, 0
    if fa * fb > 0:
        raise ValueError("interval does not bracket a root")
    n = 0
    while (b - a) / 2 > tol and n < max_iter:
        mid = (a + b) / 2
        fm = f(mid)
        if fm == 0:
            return mid, n
        if fa * fm < 0:
            b, fb = mid, fm
        else:
            a, fa = mid, fm
        n += 1
    return (a + b) / 2, n


def newton_solve(f, df, x0, eps=1e-8, itmax=50):
    """牛顿法。eps 语义：|f(x)| <= eps（单位是 f(x)）。

    收敛返回 root(float)；零导数、超 itmax 一律返回 None（无法区分原因）。
    """
    x = x0
    for _ in range(itmax):
        fx = f(x)
        if abs(fx) <= eps:
            return x
        dfx = df(x)
        if dfx == 0:
            return None
        x = x - fx / dfx
    return None


def secant_solve(f, x0, x1, delta=1e-6, nmax=100):
    """割线法。delta 语义：|x_{k+1} - x_k| <= delta（步长，单位是 x）。

    返回 {"root": r, "code": c, "iters": k}：
      code 0 = 收敛, 1 = 超 nmax, 2 = 分母为零。
    迭代计数从 1 起（初始点对算第 1 次）。
    """
    f0, f1 = f(x0), f(x1)
    for k in range(1, nmax + 1):
        denom = f1 - f0
        if denom == 0:
            return {"root": x1, "code": 2, "iters": k}
        x2 = x1 - f1 * (x1 - x0) / denom
        if abs(x2 - x1) <= delta:
            return {"root": x2, "code": 0, "iters": k}
        x0, f0, x1, f1 = x1, f1, x2, f(x2)
    return {"root": x1, "code": 1, "iters": nmax}


def fixed_point_iter(g, x0, tol=1e-6, maxit=200):
    """不动点迭代 x = g(x)。tol 语义：|x_{k+1} - x_k| <= tol。

    返回完整迭代列表。是否收敛、是否发散（nan/inf）全由调用方自行判断。
    """
    xs = [x0]
    for _ in range(maxit):
        try:
            nxt = g(xs[-1])
        except (OverflowError, ZeroDivisionError):
            xs.append(math.inf)
            break
        xs.append(nxt)
        if isinstance(nxt, float) and (math.isnan(nxt) or math.isinf(nxt)):
            break
        if abs(xs[-1] - xs[-2]) <= tol:
            break
    return xs
