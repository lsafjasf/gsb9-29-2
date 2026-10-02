# fuzz_np1 — 结构感知、种子可复现的报文变异模糊测试框架

纯 Python 3 标准库实现（`dataclasses / struct / random / multiprocessing /
unittest / hashlib / json / argparse`），无第三方依赖。

## 目标协议 NP1 与被测解析器

NP1 是一个类型化、长度前缀、可嵌套的二进制协议（见 `protocol.py` 头注释）：

```
packet = MAGIC(2) VERSION(1) FLAGS(1) COUNT(u16) NODE{COUNT}
node   = TAG(1) LEN(u32) PAYLOAD[LEN]      TAG ∈ {U32, BLOB, LIST, STRUCT}
```

`sut.py` 是一个"信任报文字段"风格的被测解析器，内嵌 4 类真实缺陷：

| 缺陷类别 | 触发方式 | 表现 |
|---|---|---|
| `length_desync` | LEN / COUNT 字段与内容不符 | `AssertionError` |
| `depth_overflow` | 嵌套深度 > 800 | `RecursionError` |
| `hang_sentinel` | U32 值 = `0xDEADBEEF`（历史魔数） | 死循环 → 超时 |
| `oversize_alloc` | BLOB 声明长度 > 4 MiB | `MemoryError` |

合法 NP1 报文全部正常解析；`ProtocolError` 属于预期拒绝，不计入检出。

## 框架结构

- `protocol.py` — 协议模型：`Document/Node` 树、序列化（支持 LEN/COUNT
  pin 篡改）、合法种子语料（small / medium / header_only / deep / wide / blob）。
- `mutator.py` — 结构感知变异。每种变异是一个可 JSON 序列化的 spec 元组，
  作用于类型化报文树：
  - 长度字段篡改：`len_pin_abs`（0/1/0x7F/0xFF/0xFFFF/0x10000/0xFFFFFFFF）、
    `len_pin_delta`（±1/±16）、`count_pin_abs`（STRUCT 子计数与根 COUNT）；
  - 边界值替换：`u32_boundary`（13 个边界值）、`blob_pattern`（空/全0/全FF/
    伪装头等 6 种）；
  - 字段重排：`reorder`（swap01 / reverse / rotate）；
  - 嵌套增删：`wrap` / `flatten` / `drop` / `insert`；
  - 线级字节变异：`truncate` / `append` / `duptail` / `flipbyte` / `insertbyte`。
  生成器：`single_doc_variants`（全部单点变异）、`pair_doc_variants`
  （全部 2 -wise 组合，可确定性步进截断）、`wire_variants`、
  `random_variants`（1–3 次树编辑 + 0–2 次线级编辑，由 `random.Random(seed)`
  驱动）。
- `runner.py` — `SafeRunner`：在 fork 出的 worker 进程中执行解析器，
  父进程按超时 `poll`；崩溃按异常类归类，超时后 kill 并自动重启 worker。
- `minimizer.py` — 失败保持的最小化：结构化收缩（删子节点/截断 BLOB）
  + 经典 ddmin 字节块消除；oracle 以"相同缺陷类别（或仍然超时）"为准。
- `demo.py` — 活动驱动：4 个 bucket（special / single / pairs / random），
  统计检出率、验证可复现性、最小化并落盘全部产物。
- `../tests/test_fuzz_np1.py` — 19 个 unittest 自测。

## 运行方式

```bash
# 自测（约 1 秒）
python3 -m unittest tests.test_fuzz_np1 -v

# 完整活动（默认种子 20261003，约 4 秒）
python3 -m fuzz_np1.demo --out artifacts

# 快速冒烟 / 自定义
python3 -m fuzz_np1.demo --quick
python3 -m fuzz_np1.demo --seed 42 --pair-cap 8000 --random-count 500 --timeout 0.3
```

产物：`artifacts/report.json`（检出率 + 复现性哈希 + 发现清单）、
`artifacts/findings/*.bin`（原始触发样本）、`*.min.bin`（最小复现样本）、
`*.json`（变异 spec、尺寸、SHA-256、复确认结果）。

## 实测数据（master seed = 20261003，worker 超时 0.25 s）

| bucket | 样本数 | ok | 预期拒绝 | 崩溃 | 超时 | 检出率 |
|---|---|---|---|---|---|---|
| special（空/超大/极深/哨兵） | 6 | 2 | 1 | 2 | 1 | 50.00% |
| single（全部单点结构变异 + 线级） | 1750 | 935 | 294 | 521 | 0 | 29.77% |
| pairs（全部 2-wise 变异组合） | 6563 | 1987 | 1360 | 3216 | 0 | 49.00% |
| random（种子驱动随机管线） | 1800 | 465 | 990 | 345 | 0 | 19.17% |

- 唯一缺陷类别检出：**4/4**（length_desync、depth_overflow、
  oversize_alloc、hang_sentinel）。
- 空样本（0 字节）、仅头部空报文、2 MiB 合法超大报文、深度 1500 嵌套、
  全部变异组合（pairs bucket 即 2-wise 全组合）均已覆盖并分类正确。

## 可复现性

- 所有变异由 `(master_seed, 种子样本)` 确定：随机管线使用
  `random.Random("<seed>:<seed_name>")`，枚举型变异顺序固定；
- `report.json.reproducibility.variant_stream_sha256` 记录各 bucket 变异流的
  SHA-256，框架在同一进程内做第二遍独立重放比对（`second_pass_identical:
  true`）；
- 跨进程验证：相同种子连跑两次，`diff -r artifacts artifacts_rerun`
  逐字节一致（已实测通过）；
- 每个最小样本落盘前都用 runner 复确认（`minimized_samples_reconfirmed:
  true`），自测另含"同种子三次流一致、异种子流不同"用例。

## 最小复现样本样例（hex）

```
length_desync   11B  4e50 0100 0003 01 00000063          # U32 声明 LEN=0x63≠4
oversize_alloc  11B  4e50 0100 0001 02 ffffffff 41      # BLOB 声明 LEN=0xFFFFFFFF
hang_sentinel   15B  4e50 0100 0003 01 00000004 deadbeef# U32 值=历史魔数
depth_overflow 4080B 4e50 0101 0002 (03 00001dxx)*…     # 嵌套 LIST 深度>800
```

（`depth_overflow` 的理论下限约为 801 层 × 5 字节 ≈ 4 KB，ddmin 已收敛到
该量级；其余三类均已收敛到字节级最小。）
