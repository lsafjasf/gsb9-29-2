# STM — 纯标准库 Python 软件事务性内存

以事务方式声明读写，提交时自动校验冲突并整体回滚、自动重试，替代手写锁。

## 文件

- `stm.py` — 库本体（仅标准库）
- `test_stm.py` — 自测（unittest，12 个用例）
- `benchmark.py` — STM vs 细粒度锁对比基准

## 运行

```bash
cd stm
python3 test_stm.py     # 或 python3 -m unittest test_stm -v
python3 benchmark.py    # 输出冲突率 / 重试次数 / 提交延迟 / 与锁方案对比
```

## 用法

```python
import stm

a, b = stm.TVar(100), stm.TVar(0)

def transfer(tx):
    tx.write(a, tx.read(a) - 10)
    tx.write(b, tx.read(b) + 10)

stm.atomically(transfer, max_retries=100)   # 冲突自动重试

# 嵌套事务: 内层放弃只回滚内层，不影响外层已做的改动
def outer(tx):
    tx.write(a, 1)
    with stm.transaction(tx) as inner:
        inner.write(b, 2)
        inner.abort()                        # 仅丢弃 b=2，a=1 保留
stm.atomically(outer)

# 显式放弃（顶层）: 整体回滚且不重试
def giveup(tx):
    tx.abort()                               # 抛出 AbortTransaction
```

## 语义

- **读集/写集**：事务记录每个 `TVar` 读取时的版本号与待写入的新值。
- **提交校验**：在全局提交锁内校验读集版本全部未变；通过则整体应用写集，
  否则抛 `ConflictError`，写集整体丢弃（无部分写入）。
- **自动重试**：`atomically` 捕获冲突后带指数退避重试；超过
  `max_retries` 抛 `RetryLimitExceeded`。
- **嵌套**：内层正常结束则读/写集合并进外层（对外仍是一次原子提交）；
  内层 `abort()` 只丢弃内层写集；外层 `abort()` 则全部回滚。
- **统计**：`stm.get_stats()` 返回 commits / conflicts / retries /
  conflict_rate / avg_retries_per_commit / 提交延迟 avg、p99。

## 测试覆盖

| 情形 | 用例 |
|---|---|
| 单事务读写、读己之写 | `SingleTransactionTest` |
| 回滚断言（显式放弃/冲突均无部分写入） | `RollbackAssertionTest` |
| 嵌套提交合并、嵌套回滚保留外层、外层放弃全回滚、三层嵌套 | `NestedTransactionTest` |
| 无冲突并发（8 线程不相交，零冲突） | `ConcurrencyTest.test_no_conflict_concurrency` |
| 高冲突热点（共享计数器无丢失更新） | `ConcurrencyTest.test_high_contention_hotspot_no_lost_update` |
| 重试上限耗尽 / 重试后成功 | `RetryLimitTest` |

## 基准结论（本机 8 线程 × 2000 次转账）

- 无冲突场景：STM 冲突率 0%，重试 0 次，提交延迟约 1µs；
  吞吐低于细粒度锁（STM 有读集记录与校验开销）。
- 热点场景（事务内含 0.1ms 业务耗时）：STM 冲突率约 8%，
  平均每次提交重试约 0.09 次，吞吐与细粒度锁相当（锁在热点下同样串行化）。
- 两方案均通过转账总额守恒校验。
