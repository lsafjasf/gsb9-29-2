# 调用点清单

重构后所有抽样调用统一走 `sampling` 组件（`Sampler` + 策略 + 注入式随机源）。
`legacy/` 为重构前原始实现的冻结快照，仅作差分测试基准，不再被业务代码引用。

## 调用点对照

| # | 调用点（新） | 原实现（冻结） | 策略 | 随机源（旧 → 新） | 参数语义变化 |
|---|---|---|---|---|---|
| 1 | `services/bi_report.py:14` `sample_rows(rows, k, rng)` | `legacy/bi_report.py:6` | `UniformSampling` | 全局 `random`（不可复现）→ 显式注入 | 无 |
| 2 | `services/etl_export.py:14` `sample_records(records, weights, k, rng)` | `legacy/etl_export.py:7` | `WeightedSampling` | 内部新建 `random.Random()`（默认不可复现）→ 显式注入 | 无 |
| 3 | `services/training_data.py:13` `sample_stratified(records, key_fn, frac, rng)` | `legacy/training_data.py:4` | `StratifiedSampling` | 裸 `rand_fn` → `FuncSource` 适配注入 | 组件统一为条数 `k`；本调用点保留 `frac` 比例语义，边界处换算 `k = round(frac * n)` |

## 差分测试（同一随机源下新旧结果逐位一致）

- `tests/test_differential.py::TestDifferentialBIReport` — 调用点 1，6 个种子 × 5 组 (n, k)
- `tests/test_differential.py::TestDifferentialETLExport` — 调用点 2，6 个种子 × 4 组权重（含零权重）× 4 个 k
- `tests/test_differential.py::TestDifferentialTrainingData` — 调用点 3，6 个种子 × 5 个 frac
- `tests/test_differential.py::TestCrossModuleConsistency` — 收敛目标：同一随机源下跨模块结果一致

## 有意的行为变化（仅限非法输入区，差分测试不覆盖）

| 情形 | 旧行为 | 新行为 |
|---|---|---|
| k > n（无放回） | 底层 `ValueError`/`IndexError` | 统一抛 `SampleSizeError`（三个策略一致） |
| 负权重 | 静默清零 | 抛 `WeightError` |
| 全零权重 / 剩余权重全零 | `IndexError` 崩溃 | 抛 `WeightError` |
| 权重含 NaN/Inf、长度不匹配 | 未定义 | 抛 `WeightError` |
| 未注入随机源 / 随机源产出越界 | 未定义 | 抛 `RandomSourceUnavailable` |
| 流式输入 k > n | 不适用（新能力） | 不报错，返回全部可用条目（clamp，见 `sampling/stream.py` 文档） |
