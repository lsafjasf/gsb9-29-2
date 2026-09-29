# WFQ — 加权公平队列（Python 3，仅标准库）

多类流量共用单条出口链路，按权重分配带宽；某类空闲时其份额立即被活跃队列占用（work-conserving）。

## 算法

SCFQ（Self-Clocked Fair Queuing，WFQ 族）：每个队首包计算虚拟完成标签

```
F = max(V, F_prev_of_queue) + L / w
```

`V` 为虚拟时间（当前/最近在服务包的完成标签），`L` 为包长，`w` 为队列权重。
服务器总是服务 `F` 最小的队首包（非抢占、逐包、事件驱动仿真）。

- 空队列不在可调度集合中 → 其份额**立即**被积压队列占满；
- 队列空闲后重新到达时 `F` 与当前虚拟时间取 max → 空闲期间不攒"配额"，回归后无法独占链路；
- 权重 0 合法，语义为 best-effort：仅当无正权重队列可调度时才被服务。

## 文件

- `wfq.py` — 库：`WFQSimulator`（建队/提交包/运行）+ `Stats`（份额、窗口吞吐、最大等待、最大服务间隔）
- `test_wfq.py` — 13 个 unittest 用例
- `demo.py` — 5 个场景，打印带宽比例与最大等待/最大服务间隔数据

## 运行

```bash
cd wfq
python3 demo.py          # 打印比例与等待数据
python3 -m unittest -v   # 运行自测
```

## 测试覆盖

- 单队列占满链路；权重 3:1、1:2:3:4 的比例精度
- 权重为零：独占时跑满、有正权重队列时让路、正权重队列排空后立即占满
- 空闲队列份额即时回收（`test_idle_share_instantly_reclaimed`）；回归队列不可独占（`test_returning_queue_cannot_monopolize`）
- 权重悬殊 1:100 + 全突发：不饿死，最大服务间隔 ≤ W/w + 1 个包服务时间（`test_no_starvation_extreme_weights_burst`）
- 全部队列同时突发（`test_simultaneous_burst_all_queues`）
- 队列频繁进出/churn（`test_queues_frequently_joining_and_leaving`）
- 非法配置：负权重、未知队列、重复队列名

## 关键指标说明

- `share(q, t0, t1)` — 窗口内 q 的字节占比（公平性须在"相关队列均积压"的窗口内测量）
- `max_wait(q)` — 最坏"到达→开始服务"时延
- `max_service_gap(q)` — 相邻两次被服务的最大间隔，即"不饿死"指标；权重 w、总权重 W 时上界约 W/w 个包服务时间
