# 调用点改造清单

旧调用点在 `legacy/callers.py`，新调用点在 `callers_new.py`。
所有“按求解器定制”的分支已删除，统一为 `_wrap(SolveResult)`。

| # | 调用点 | 旧适配代码（已删除） | 新写法 | 状态 |
|---|---|---|---|---|
| 1 | `solve_sqrt2` (bisection) | `try/except ValueError` 兜非法区间；无法识别超限 | `Problem(method="bisection", f=..., bracket=(0,2))` + `tol=1e-8, max_iter=200` | ✅ 已改造，差分测试通过 |
| 2 | `solve_cubic` (newton) | `if r is None` 笼统判失败（零导数/超限不分） | `Problem(method="newton", f=..., df=..., x0=1.5)` + `tol=1e-10, max_iter=50` | ✅ 已改造，解逐位一致 |
| 3 | `solve_cos` (secant) | 解读自定义 `code` 0/1/2 | `Problem(method="secant", f=..., x0=0, x1=1)` + `tol=1e-10, max_iter=100` | ✅ 已改造，差分测试通过 |
| 4 | `solve_exp_decay` (fixed_point) | 手翻迭代列表判 `nan/inf` 与末两项间距 | `Problem(method="fixed_point", g=..., x0=0.5)` + `tol=1e-10, max_iter=500` | ✅ 已改造，差分测试通过 |

## 迁移步骤（新增调用点时照做）

1. 把求解器入参翻译为 `Problem(method=..., f/df/g=..., bracket/x0/x1=...)`。
2. 按 `docs/PARAMETER_MAPPING.md` 把旧容差换算为残差容差 `tol`，确认 `max_iter` 计数口径。
3. 用 `result.reason` 替换所有 try/except、None 判断、code 解读、列表检查。
4. 在 `tests/test_differential.py` 增加对拍用例：同问题跑新旧两条路径，
   断言解差 `<= tol`、残差达标、停止原因符合映射表。
5. 验证通过后删除旧调用分支。

## 验证

- 差分测试：`tests/test_differential.py`（16 例：4 调用点 + 4 求解器成功路径 + 8 停止原因映射）
- 回归测试：`tests/test_unified.py`（20 例：收敛/超限/发散/输入非法全覆盖）
