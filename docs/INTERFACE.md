# 统一求解器接口契约

## 入口

```python
from solver import RootProblem, StopReason, solve

result = solve(problem, tol=1e-8, max_iter=100, method="auto")
```

* `solve` **永远不抛求解语义异常**：非法输入、发散、超限都通过返回值表达。
  只有真正的编程错误（例如传入对象根本不是 `RootProblem`、`f` 不可调用）
  仍以普通 Python 异常或 `INVALID_INPUT` 的方式暴露。
* `method`：`"bisection"` / `"newton"` / `"fixedpoint"` / `"auto"`。
  `auto` 的选择顺序：提供 `fp + x0` → Newton；否则提供 `bracket` → 二分；
  否则提供 `g + x0` → 不动点。

## 问题描述 RootProblem

| 字段 | 含义 | 适用方法 |
|---|---|---|
| `f(x)` | 残差函数，求解 f(x)=0 | 全部（`g` 提供时自动补 `f(x)=g(x)-x`） |
| `fp(x)` | f 的导数 | newton |
| `bracket=(a,b)` | 变号区间，要求 a<b 且 f(a)·f(b)≤0 | bisection |
| `x0` | 初值（有限实数） | newton / fixedpoint |
| `g(x)` | 不动点映射，残差为 g(x)-x | fixedpoint |

## 统一后的参数语义

| 参数 | 统一语义 | 单位 |
|---|---|---|
| `tol` | **绝对残差容差**。收敛判据：Newton/二分 `|f(x)| ≤ tol`；不动点 `|g(x)-x| ≤ tol` | 与残差同单位（f 的单位，不是 x 的单位） |
| `max_iter` | 迭代次数上限（正整数）。一次迭代 = 一次中点求值（二分）或一次状态更新 x←…（Newton/不动点）。初始点 x0 为第 0 次迭代，不计入上限 | 次 |

## 返回值 SolveResult

`SolveResult(reason, x, residual, iterations, message)`，不可变 dataclass：

| 字段 | 内容 |
|---|---|
| `reason` | `StopReason` 枚举，闭集，四选一（见下） |
| `x` | 最后一个迭代点（非法输入时为 `None`） |
| `residual` | x 处残差绝对值；发散且无法求值时为 `NaN`；非法输入为 `None` |
| `iterations` | 实际执行的迭代次数（0 = 初值即满足，或输入非法未开始） |
| `message` | 人类可读的停止细节（英文，稳定分类用 `reason` 不要匹配字符串） |
| `.converged` | 便捷属性，等价于 `reason is StopReason.CONVERGED` |

## 停止原因（闭集枚举）

| reason | 触发条件 |
|---|---|
| `CONVERGED` | 残差达到 `tol`（含区间端点恰为根、初值即满足） |
| `MAX_ITER` | 执行满 `max_iter` 次迭代仍未收敛；返回最后的 x 与残差 |
| `DIVERGED` | 序列出现 NaN/Inf 或越过防爆阈值 `|x| > 1e12`；Newton 导数为 0；残差函数在**迭代途中**抛 `ValueError`/`ArithmeticError` |
| `INVALID_INPUT` | `tol ≤ 0`/非有限、`max_iter < 1`/非整数、布尔值冒充实数、不可调用、缺少方法所需字段、区间不变号、端点非有限、残差函数在**端点或初值处**抛错 |

端点/初值处的函数错误归 `INVALID_INPUT`（问题没定义在声称的区间上），
迭代途中才暴露的错误归 `DIVERGED`（出发点合法但迭代跑出定义域）。
