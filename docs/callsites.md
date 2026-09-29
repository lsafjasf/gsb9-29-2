# 调用点清单

三个原调用点全部收敛到 `sampling` 统一组件；随机源通过关键字参数 `rng` 显式注入。

| 调用点 | 遗留实现 | 重构后 | 统一策略 | 随机源（遗留 → 现在） | 差分结果 |
|---|---|---|---|---|---|
| 抽奖 `pick_winners(records, k)` | `legacy/lottery.py` | `callsites/lottery.py` | `UniformSampling` | 全局 `random` 模块 → 注入 `rng`（对齐时用 `ModuleSource`） | 200 用例 0 不一致 |
| 促销 `draw_by_weight(items, weights, k)` | `legacy/promo.py` | `callsites/promo.py` | `WeightedSampling(replace=True)` | 每次调用新建无种子 `Random()` → 注入 `rng` | 200 用例 0 不一致 |
| 报表 `stratified_sample(rows, key, k)` | `legacy/report.py` | `callsites/report.py` | `StratifiedSampling` | 硬编码种子 2024 → 注入 `rng` | 全部用例 0 不一致 |

## 有意的行为差异（修复遗留缺陷，均有回归测试钉住）

| 场景 | 遗留行为 | 统一组件行为 |
|---|---|---|
| `k > 总体量`（lottery） | 静默返回全部记录 | 抛 `SampleSizeError`（所有不放回策略语义一致） |
| 分层配额含小数（report） | `int()` 截断，返回行数 < k | 最大余数法，配额之和恒等于 k |
| 零权重项且随机源产出 0.0（promo） | `<=` 比较导致零权重项可被选中 | 严格 `<` 扫描，零权重项不可达 |
| 权重全为零（promo） | `ZeroDivisionError` | 抛 `WeightError` |

差分细节见 `reports/differential.md`（由 `tools/differential_report.py` 生成）。
