# 调用点改造清单

三个真实调用点均已改造，新旧版本保留在仓库中供差分对比：

| # | 调用点 | legacy 文件 | 重构后文件 | 使用方法 |
|---|---|---|---|---|
| 1 | 储罐液位（部分填充圆柱体积求根） | `legacy/tank_level.py` | `app/tank_level.py` | bisection |
| 2 | 收益率曲线 IRR（现金流贴现求根） | `legacy/yield_curve.py` | `app/yield_curve.py` | newton |
| 3 | 均衡不动点 | `legacy/equilibrium.py` | `app/equilibrium.py` | fixedpoint |

## 统一改造步骤（每个调用点都一样）

1. 用 `RootProblem` 描述问题：`f`（残差）、`fp`（导数）、`bracket`、`x0`、`g`。
2. 调 `solve(problem, tol=, max_iter=, method=)`，**删除所有 try/except 数值
   异常与状态码分支**。
3. 只对 `result.reason`（四值枚举）做分支：`CONVERGED` 取
   `result.x/result.residual`，其余映射为该调用点的业务状态串。
4. 容差单位换算：二分调用点把原 x 空间 tol 乘根处斜率换算为残差 tol
   （见 `docs/MAPPING.md` 第 1 节）；Newton/不动点直接平移。
5. `max_iter` 语义与计数方式三引擎已统一，原值平移即可。

## 各调用点的业务状态映射

**储罐液位 `app/tank_level.py`**

| 统一 reason | 旧分支 | 新返回状态 |
|---|---|---|
| `CONVERGED` | 正常元组 | `"ok"` |
| `MAX_ITER` | 捕获 `RuntimeError`，读 `exc.last_x/last_residual` | `"no-convergence"` |
| `DIVERGED` | 旧代码无法表达（混在 `ValueError → "bad-input"` 里） | `"diverged"`（新增强化，可区分域错误） |
| `INVALID_INPUT` | 捕获 `ValueError` | `"bad-input"` |

同时残差统一取绝对值（旧版返回带符号 f(h)）。

**收益率 IRR `app/yield_curve.py`**

| 统一 reason | 旧分支 | 新返回状态 |
|---|---|---|
| `CONVERGED` | dict 解包 `result["x"]/["fx"]/["n"]` | `"ok"` |
| `MAX_ITER` | `result is None`（丢失最后迭代点） | `"did-not-converge"`（现在保留 x/残差） |
| `DIVERGED` + message 含 "derivative evaluated to zero" | `ZeroDivisionError` | `"stalled"` |
| `DIVERGED`（其它） | `FloatingPointError` | `"blew-up"` |
| `INVALID_INPUT` | `ValueError` | `"bad-guess"` |

参数 `eps` 重命名为 `tol`（数值语义相同，都是绝对残差）。

**均衡不动点 `app/equilibrium.py`**

| 统一 reason | 旧整数码 | 新返回状态 |
|---|---|---|
| `CONVERGED` | `CODE_OK = 0` | `"ok"` |
| `MAX_ITER` | `CODE_LIMIT = 1` | `"limit"` |
| `DIVERGED` | `CODE_DIVERGED = 2` | `"diverged"` |
| `INVALID_INPUT` | `CODE_BAD_INPUT = 3` | `"invalid-start"` |

## 新增调用点的检查清单

- [ ] 残差写成 `f(x)=0`（不动点写残差 `g(x)-x` 或直接给 `g`）。
- [ ] 容差按残差单位给；从宽度/步长语义迁移时做单位换算。
- [ ] 不再 try/except `ValueError/FloatingPointError/ZeroDivisionError/RuntimeError`。
- [ ] 不再判 `None`、整数码或读异常对象上的 `last_x` 之类属性。
- [ ] 四个 `StopReason` 都有明确处理（哪怕只是记日志）。
- [ ] 加一条差分用例（参照 `tests/test_differential.py` 的对应 call-site 类）。
