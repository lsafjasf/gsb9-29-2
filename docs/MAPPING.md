# 参数语义对照表（legacy → unified）

## 1. 参数对照

| 维度 | `legacy/bisect_solver.bisect` | `legacy/newton_solver.newton` | `legacy/fixedpoint_solver.FixedPointSolver` | 统一 `solve` |
|---|---|---|---|---|
| 问题描述 | 位置参数 `f, a, b` | 位置参数 `f, fp, x0` | 构造传 `g`，`solve(x0)` | `RootProblem(f=, fp=, bracket=, x0=, g=)` |
| 容差参数名 | `tol=1e-6` | `eps=1e-8` | `tol=1e-7` | `tol`（默认 1e-8） |
| 容差含义 | **x 空间**区间半宽 `(b-a)/2 < tol` | 绝对残差 `|f(x)| ≤ eps` | 绝对步长残差 `|g(x)-x| ≤ tol` | **绝对残差**：`|f(x)| ≤ tol` 或 `|g(x)-x| ≤ tol` |
| 容差单位 | x 的单位 | f 的单位 | x 的单位（残差即步长） | 残差单位（f 的单位；不动点为 x 的单位） |
| 上限参数名 | `maxit=100` | `max_iter=50` | `maxit=100` | `max_iter=100` |
| 上限计数 | 中点求值次数（=区间收缩次数） | Newton 更新次数，x0 为第 0 次 | 状态更新次数 x←g(x)，x0 为第 0 次 | 与各引擎原有计数一致：一次中点求值/一次状态更新为 1 次迭代，x0 不计数 |
| 收敛检查时机 | 更新中点后，先判残差=0 再判宽度 | 每次更新后判残差，再判爆炸 | 每次更新后判步长，再判爆炸 | 更新后判残差，再判爆炸 |
| 防爆阈值 | 无 | `|x| > 1e10` | `|x| > 1e12` | 统一 `|x| > 1e12`（含 NaN/Inf） |

### 容差迁移公式（二分调用点必读）

遗留二分的 `tol` 是 x 空间宽度，统一后 `tol` 是残差。在根附近
`f(x) ≈ f′(x*)·(x − x*)`，因此迁移为：

```
unified_tol = |f′(x*)| · legacy_tol        # 一阶近似
```

拿不到导数时用相邻点数值微分估计斜率（见 `app/tank_level.py` 调用点说明）。
Newton 与不动点的容差语义不变，数值可直接平移。

## 2. 返回值 / 失败信号对照

| 情形 | 二分 legacy | Newton legacy | 不动点 legacy | 统一 reason |
|---|---|---|---|---|
| 收敛 | 返回元组 `(x, f(x))`（f(x) 带符号） | 返回 dict `{"x","fx","n"}`（fx 带符号） | 状态码 `0`，残差为 x* 处再算一次的 `|g(x*)-x*|` | `CONVERGED`，`x`/`residual=abs(...)`/`iterations` |
| 超限 | `raise RuntimeError`，值挂在 `exc.last_x/last_residual/iterations` | **返回 `None`**（x 与残差全部丢失） | 状态码 `1`，残差是**上一步**步长（与返回点 x 错位一次迭代） | `MAX_ITER`，统一返回最后 x 与该点残差 |
| 发散 | 无专门信号；f 抛域错误时表现为 `ValueError` | `raise FloatingPointError`（爆炸）/ `ZeroDivisionError`（导数为 0） | 状态码 `2` | `DIVERGED`（含爆炸、NaN/Inf、导数为 0、迭代途中域错误），细节在 `message` |
| 输入非法 | `raise ValueError`（与迭代途中域错误**同一种异常**，调用方无法区分） | `raise ValueError`（初值非法）/ `TypeError`（不可调用） | 状态码 `3` | `INVALID_INPUT`，`x=None, residual=None, iterations=0` |

## 3. 停止原因分类对应（差分测试依据）

| 统一 reason | 二分 legacy 信号 | Newton legacy 信号 | 不动点 legacy 信号 |
|---|---|---|---|
| `CONVERGED` | 正常返回元组 | 正常返回 dict | code 0 |
| `MAX_ITER` | `RuntimeError` | `None` | code 1 |
| `DIVERGED` | `ValueError`（f 在中点求值时抛错） | `FloatingPointError`、`ZeroDivisionError` | code 2 |
| `INVALID_INPUT` | `ValueError`（参数/区间错误，或 f 在端点 a/b 处抛错） | `ValueError`（x0 非法）、`TypeError` | code 3 |

注意二分 legacy 用**同一个 `ValueError`** 同时表达"输入非法"和"迭代途中
发散"，这是旧接口最危险的歧义；统一后按"出错位置在端点还是中点"拆成
`INVALID_INPUT` 与 `DIVERGED`，由 `tests/test_differential.py` 锁定。

## 4. 差分测试中的等价判据

* Newton / 不动点收敛路径：`x` 与残差**逐位相等**（同一浮点运算序列）。
* 二分收敛路径：容差语义不同（宽度 vs 残差），两边通常在相邻迭代停机；
  判据为 `|x_new − x_old| ≤ legacy_tol` 且两边残差均落入映射后的容差带。
  当根恰为某次中点时，两边逐位相等（如半满罐体 h=r=1）。
* 任何方法的超限路径：reason 分类必须对应；二分的中点 x 与残差逐位相等；
  不动点常步长场景（如 g(x)=x+1）逐位相等，一般映射的残差相差一次 g 求值。
* 发散路径：reason 分类对应；不动点在共同阈值 1e12 下 x 逐位相等；Newton
  阈值由 1e10 抬到 1e12，只断言分类（爆炸是预期行为，具体爆炸点不属于
  需要保持的契约）。
* 非法输入：reason 分类对应，调用点对外载荷（x/residual）相等。
