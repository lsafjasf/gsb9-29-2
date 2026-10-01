# 参数语义对照表（旧 -> 统一）

## 统一语义（新接口 `unified.solve(problem, *, tol, max_iter)`）

| 参数 | 语义 | 单位 | 合法值 |
|---|---|---|---|
| `tol` | 绝对残差容差，收敛判据 `\|f(x)\| <= tol`（不动点为 `\|g(x)-x\| <= tol`） | 与 f(x) 相同 | 有限正实数 |
| `max_iter` | 允许的最大“迭代更新步数”，每产生一个新 x 计 1 次，初始点计 0 | 次 | 正整数 |

返回 `SolveResult{x, residual, reason, iterations, method, message}`，
`reason ∈ {CONVERGED, MAX_ITER_EXCEEDED, DIVERGED, INVALID_INPUT}`，任何路径不抛异常。

## 容差语义对照

| 求解器 | 旧参数 | 旧判定 | 旧单位 | 统一后 | 换算关系 |
|---|---|---|---|---|---|
| `bisect_solve` | `tol` | 区间半宽 `(b-a)/2 <= tol` | x | 残差 `\|f(x)\| <= tol` | `tol_new ≈ \|f'(ξ)\| · tol_old` |
| `newton_solve` | `eps` | `\|f(x)\| <= eps` | f(x) | 相同 | 直接对应 `tol = eps` |
| `secant_solve` | `delta` | 步长 `\|x_{k+1}-x_k\| <= delta` | x | 残差 | `tol_new ≈ \|f'(ξ)\| · delta_old` |
| `fixed_point_iter` | `tol` | 步长 `\|x_{k+1}-x_k\| <= tol` | x | 残差 `\|g(x)-x\| <= tol` | 等价（同一量的相邻迭代形式） |

## 迭代上限计数对照

| 求解器 | 旧计数方式 | 统一计数方式 |
|---|---|---|
| `bisect_solve` | `n` = 区间减半次数 | 相同（每次中点评估计 1 次） |
| `newton_solve` | `itmax` = 最大步数 | 相同 |
| `secant_solve` | 从 1 起计，初始点对算第 1 次 | 从 0 起计，初始点计 0，每个新 x2 计 1 次 |
| `fixed_point_iter` | `maxit` = 最大 g 调用次数 | 相同（每次更新调用一次 g） |

## 停止原因映射

| 求解器 | 旧失败表示 | 统一 `reason` |
|---|---|---|
| `bisect_solve` | 收敛返回 `(root, n)` | `CONVERGED` |
| `bisect_solve` | 超限静默返回且 `n == max_iter`（无法区分） | `MAX_ITER_EXCEEDED` |
| `bisect_solve` | 抛 `ValueError`（区间不夹根） | `INVALID_INPUT` |
| `newton_solve` | 返回 float | `CONVERGED` |
| `newton_solve` | 返回 `None`（零导数） | `DIVERGED` |
| `newton_solve` | 返回 `None`（超限，与零导数无法区分） | `MAX_ITER_EXCEEDED` |
| `secant_solve` | `code == 0` | `CONVERGED` |
| `secant_solve` | `code == 1` | `MAX_ITER_EXCEEDED` |
| `secant_solve` | `code == 2`（分母为零） | `DIVERGED` |
| `fixed_point_iter` | 末两项间距 `<= tol` | `CONVERGED` |
| `fixed_point_iter` | 迭代列表含 `nan/inf` | `DIVERGED` |
| `fixed_point_iter` | 列表用尽仍不收敛 | `MAX_ITER_EXCEEDED` |
| 全部 | （旧接口无此概念） | `INVALID_INPUT`：tol/max_iter 非法、缺字段、区间不夹根等 |

## 残差定义

| 方法 | `residual` |
|---|---|
| bisection / newton / secant | `\|f(x)\|` |
| fixed_point | `\|g(x) - x\|` |
