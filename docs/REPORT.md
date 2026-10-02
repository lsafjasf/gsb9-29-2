# 告警规则静态检查报告

纯标准库 Python 3 实现（`alertlint/`），对告警规则做两类静态判定：

1. **可触发性**：结合指标注册表中的取值范围 `[min, max]`、聚合方式与窗口长度，
   推导出每条条件的**触发域**（满足条件的聚合值集合），再与该聚合序列的
   **可行域**求交：
   - 交集为空 → `NEVER_FIRES`（永远不触发）
   - 交集覆盖整个可行域 → `ALWAYS_FIRES`（永远触发）
   - 其余 → 正常
2. **重复 / 重叠**：规则先做条件集的规范化（条件顺序无关），再按
   `(metric, agg, window)` 序列对触发域做 **Jaccard 相似度**与**包含关系**
   计算，输出等价、包含、高度重叠三类结论及量化依据。

所有结论都是**可证明**的：信息不足（指标未注册、`sum` 无采样间隔）时只给
`UNKNOWN`，绝不判死/判恒真，这是误报控制的基础。

## 判定依据（推导方法）

### 可行域推导

| 聚合 | 可行域 | 说明 |
|---|---|---|
| `avg / min / max / last / p50/p90/p95/p99` | `[min, max]` | 任意窗口长度下输出都不会超出原始样本范围 |
| `sum` | `[min·n, max·n]`，`n = window / sample_interval_seconds` | 按窗口内采样点数线性放大；未配置采样间隔时视为无界，**不下任何结论** |

### 单条件触发域（区间支持开/闭端点）

| 比较 | 触发域 |
|---|---|
| `gt t` | `(t, +∞)` |
| `ge t` | `[t, +∞)` |
| `lt t` | `(-∞, t)` |
| `le t` | `(-∞, t]` |
| `eq t` | `{t}` |
| `ne t` | 实数域去掉点 `{t}` |

开闭端点的区分直接决定阈值边界的正确性：`cpu > 100`（范围 `[0,100]`）交集为空
（永死），而 `cpu >= 100` 交集为点 `{100}`（可触发）。

### 组合条件

- **AND**：按 `(metric, agg, window)` 分组求触发域交集；任一组为空 → 永死
  （同指标矛盾）；每组交集都覆盖其可行域 → 恒触发。
- **OR**：每个分支均不可满足 → 永死；同一序列上的分支并集（含端点相接的
  闭/开区间合并）覆盖整个可行域 → 恒触发。
- 组间为相互独立的指标序列时，不做跨指标的"互补覆盖"推断（如 `cpu>90 OR
  mem>90` 不能证明恒真），避免误报。
- 含未知指标的分支不参与死/恒真判定；若已知分支已可独立证明永死，则永死结论
  仍然成立。

### 重复归并

- **等价（1.0）**：规范化条件元组 `(metric, agg, window, op, threshold)` 后
  集合完全相同（AND 条件顺序无关；单条件规则忽略 combinator）；或同一组序列
  上触发域 Jaccard = 1.0。
- **包含**：AND 规则 A 的条件集是 B 的真子集，则 B 触发 ⇒ A 触发；依据为
  条件集 Jaccard。**注意：阈值分层（warning 90 / critical 99）是合法模式，
  仅当相似度足够高（默认 0.8）或条件集包含度 ≥ 0.5 才提示**，且包含类只报
  `info` 级。
- **高度重叠**：共享完全相同的序列集合时，逐序列计算触发域 Jaccard，AND 取
  最小、OR 取最大、混合取均值，≥ 阈值（默认 0.8）才报。序列集合不同的规则
  （如 `node_down` 与 `errors OR node_down`）不按重叠上报，避免误报。
- 两条永死规则（触发域均为空）不互相记为重复——它们各自已经是 `NEVER_FIRES`。

## 问题清单（`examples/rules.json`，17 条规则）

运行：`python3 -m alertlint --metrics examples/metrics.json --rules examples/rules.json`
机读结果：`examples/report.json`。共 11 条问题（3 永死、3 恒触发、1 缺失指标、
4 重复/包含）。

| 规则 | 级别 | 结论 | 依据 |
|---|---|---|---|
| R-002 `avg(cpu,300s) > 150` | error | 永不触发 | 触发域 `(150,+∞)` 与可行域 `[0,100]` 不相交 |
| R-003 `avg(mem,600s) >= 0` | error | 恒触发 | 触发域覆盖整个可行域 `[0,100]` |
| R-010 `cpu>80 AND cpu<20`（同窗口） | error | 永不触发 | AND 矛盾：`(80,100] ∩ [0,20) = ∅` |
| R-011 `disk>60 OR disk<=60` | error | 恒触发 | 两支并集覆盖 `[0,100]` |
| R-012 引用 `phantom_queue_lag` | warning | 指标缺失 | 不在注册表，判定为 UNKNOWN 而非误报永死 |
| R-014 `sum(rps,24h) > 8e7` | error | 永不触发 | n=1440，可行域 `[0,7.2e7]`，阈值超出 |
| R-015 `max(cpu,1h) >= 0` | error | 恒触发 | 覆盖整个可行域 |
| R-001 ↔ R-017 | info | 包含 | R-017 条件集 {cpu>90, mem>90} ⊃ {cpu>90}，R-017 触发⇒R-001 触发，条件集 Jaccard 1/2 |
| R-005 ↔ R-006 | warning | 等价 | 规范化条件元组完全相同，相似度 1.00 |
| R-005/R-006 ↔ R-007 | warning | 高度重叠 | 触发域 `(0.05,1]` 与 `(0.055,1]`，Jaccard 0.9947 |

合法规则未受影响：`node_up<1`（低阈值宕机告警）、24h 窗口规则、
`error_ratio>0.05` 正常告警、`sum(rps,24h)>5e7`（n=1440，阈值 5e7 < 7.2e7，
可触发）均判为正常。

## 误报数据（FP / Recall 控制集）

两个带标签的语料固化在仓库中，由 `tests/test_false_positives.py` 断言，
可重复测量：

| 语料 | 文件 | 规则数 | 期望 | 实测 |
|---|---|---|---|---|
| 合法规则（误报控制集） | `examples/legit_rules.json` | 14 | 0 条 NEVER/ALWAYS/DUP | **0，误报率 0/14 = 0%** |
| 埋雷规则（召回控制集） | `examples/bad_rules.json` | 16 | 全部检出 | **16/16 = 100%** |

合法语料刻意覆盖了最容易误伤的形态（全部判 OK）：

- 极低阈值：`http_error_ratio > 0.001`（任意错误都报）
- 近界阈值：`temperature < -30`（范围 `[-40,85]`，可行）、`cpu > 97`
- 长窗口：`avg(cpu, 24h) > 60`、`avg(mem, 24h) > 85`（窗口不改变值域）
- 长窗口求和：`sum(rps, 24h) > 5e7`（按采样点数缩放，不误判为不可达）
- 阈值分层：`cpu>90` warning vs `cpu>97` critical（Jaccard 0.3，不报）；
  `disk>85` vs `disk>93`（Jaccard 0.47，不报）
- 组合条件：`cpu>95 AND mem>95`、`error_ratio>0.5 OR node_up<1`

埋雷语料覆盖：上界外阈值、下界恒真、严格边界（`>max` / `<=max`）、
`eq` 域外值、AND 自矛盾、OR 互补恒真、OR 全死、精确重复、近似重复
（Jaccard 0.8）、条件集包含、缺失指标、`sum` 缩放后越界。

开发过程中 FP 控制集实际拦截过一次自身缺陷：早期版本把单条件 `node_down`
与多一个 OR 分支的 `errors OR node_down` 判成"等价重复"；已修正为"序列集合
不同则不按重叠上报"（见 `duplicates.py` 注释）。

## 边界用例

`tests/test_boundaries.py` 系统覆盖阈值边界：

- `gt max` → 永死；`ge max` → 可触发（仅在端点）
- `lt min` → 永死；`le min` → 可触发
- `ge min` / `le max` → 恒触发；`gt min` / `lt max` → 可触发（不恒真）
- `eq min` / `eq max` 可触发；`eq max+ε` 永死
- 恒值指标（min == max == 7）：`eq 7` 恒触发、`ne 7` 永死、`ge 7` 恒触发
- `sum` 精确边界：`gt 3e6`（= 缩放后上界）永死，`ge 3e6` 可触发
- 窗口短于采样间隔（n=0.5）仍正确缩放
- 区间原语：开区间与闭区间在端点的相交为空/为点

组合边界：`x>=90 AND x<=90` 可触发（点 90）；`x>90 AND x<=90` 永死；
`x>60 OR x<=60` 恒真；`x>60 OR x<60` 不恒真（点 60 静默）。

## 运行方式

需要 Python 3.10+（仅标准库）。

```bash
# 文本报告（有 warning/error 时退出码为 1，可接 CI）
python3 -m alertlint --metrics examples/metrics.json --rules examples/rules.json

# JSON 机读输出，可重复 --rules 合并多个文件
python3 -m alertlint --metrics examples/metrics.json \
  --rules examples/rules.json --format json > report.json

# 调整相似度阈值 / 只对 error 失败
python3 -m alertlint --metrics examples/metrics.json \
  --rules examples/rules.json --similarity-threshold 0.9 --fail-at error

# 自测（81 个用例，含误报/召回控制集）
python3 -m unittest discover -s tests -v
```

作为库使用：

```python
from alertlint import Checker
from alertlint.loader import load_metrics, load_rules

checker = Checker(load_metrics("metrics.json"))
report = checker.check(load_rules("rules.json")[0])
for issue in report.issues:
    print(issue.type, issue.rule_ids, issue.evidence)
```
