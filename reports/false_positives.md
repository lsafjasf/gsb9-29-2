# 误报控制数据

静态检查最危险的失败模式是把**正常规则误判成问题**——它会像告警风暴一样消耗信任。
本项目的原则：**推导不出来就报告"无法判定"，绝不猜**。以下数据全部由自测自动校验
（`tests/test_false_positives.py`、`tests/test_boundary_cases.py`）。

## 1. 合法规则对照组：零误报

`examples/rules_legit.json` 共 13 条刻意覆盖"容易误判"形态的合法规则，
用 `examples/metrics.json` 检查，结果（机器可读：`reports/false_positive_report.json`）：

```
规则总数 13｜恒不触发 0｜恒触发 0｜无法判定 0｜正常 13
重复组 0｜错误 0｜警告 0
```

| 规则 | 形态 | 为什么容易误判 / 工具结论 |
|---|---|---|
| ok-cpu-high | cpu avg 5m > 90 | 正常高阈值 |
| ok-cpu-high-long | cpu avg 15m > 90 | 合法长窗口；窗口 5m↔15m 相似度 0.883 < 0.92，**不**并为重复 |
| ok-cpu-warn | cpu avg 5m > 50 | 合法低阈值（相对上界 100），触发域可空可非空 |
| ok-cpu-spike | cpu max 5m > 90 | 与 avg>90 数值域相同，但**聚合不同语义不同**，封顶 0.85，不并重复 |
| ok-err-high | error_rate avg 5m > 0.8 | 正常 |
| ok-err-low | error_rate avg 5m > 0.05 | **低阈值合法规则**：[0,1] 上触发域非空，判 may_trigger |
| ok-cpu-sustained | cpu avg 5m > 90 for 2h | 持续时长不同语义不同，封顶 0.85，不与 ok-cpu-high 并重复 |
| ok-latency-p99 | latency_ms p99 5m > 500 | 无界指标：高阈值**不**判恒不触发 |
| ok-cpu-sum | cpu sum 5m > 400 | sum 域可推导 [0,500]，阈值合法 |
| ok-node-down | node_up avg 10m < 1 | 整数域 [0,1]：<1 剩取值 0，可触发 |
| ok-avail-slo | availability avg 30d < 0.999 | **窗口恰等于保留期 30d**，合法长窗口 |
| ok-latency-p99-1h | latency_ms p99 1h > 500 | 与 5m 版窗口差 12 倍，相似度 0.86 < 0.92，不并 |
| ok-5xx-rate | http_5xx_count rate 5m > 10 | 计数/速率聚合，正常 |

## 2. 边界用例中的"合法侧"：全部零 error/warning

`examples/boundary_cases.json`（30 条）里 `expect = may_trigger` 的用例，
包括以下易误报边界，检查结果全部为零 error/warning：

- 阈值恰在边界：`>=100`、`<=0`、`==100`（连续域单点可触发）
- 整数域边界：`node_up > 0`（尚有取值 1）、`node_up >= 1`
- 窗口边界：`window = retention`（等于，合法）；`window < every`（仅 info 提示）
- 无界指标：`latency_ms > 10^12`（不可推导为恒假）
- 窄但可行的组合：`cpu > 90 AND cpu < 95`
- OR 带一个死分支：`cpu > 150 OR error_rate > 0.9`

## 3. "无法判定"而非误报的场景

信息不足时工具输出 `inconclusive`（warning 级提示补元数据，**不**判问题）：

| 场景 | 输入 | 输出 |
|---|---|---|
| 指标未注册 | gpu_temp > 80 | inconclusive + missing_metric |
| 无界指标做 sum | memory_bytes sum(5m) > 1e12 | inconclusive（总量上界未知） |
| 未知聚合方式 | cpu weird_agg(5m) > 90 | 按原值域处理 + 说明，不误报 |

## 4. 防误报的推导规则（设计层面）

1. **严格开闭边界**：`>`/`<` 与 `>=`/`<=` 分开处理；整数域 `>9` 规范化为 `>=10`。
   不会把 `cpu >= 100` 误判成恒不触发。
2. **无界不猜测**：指标单侧/双侧无界（或聚合后无界）时，只在能用无界侧下结论的方向上
   给结论（如 `latency_ms >= 0` 恒真），反方向一律 inconclusive。
3. **不同的量不互相约束**：AND/OR 的跨叶子约束按 `(metric, agg, window)` 分组，
   `avg>90 AND max<80` 不会被误判为矛盾（它们是两个不同的量）。
4. **重复检测护栏**：聚合不同、`for` 不同直接封顶相似度 0.85；
   方向相反（`>80` vs `<80`）严格压分；两条都恒不触发/恒触发的规则不再报重复
   （它们已被可触发性检查覆盖）。
5. **窗口语义独立**：长窗口本身合法，只有 `window > retention`（数据填不满）才算问题；
   `window == retention`、`window < every` 分别为合法/提示。

## 5. 自动校验

```bash
python3 -m unittest discover -s tests
# 114 tests，全部通过
```

若任何一条合法规则将来被判成问题，CI 立即失败。
