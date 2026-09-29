"""STM: 纯标准库实现的软件事务性内存。

核心机制:
- 每个 TVar 携带单调递增的版本号。
- 事务记录读集 (TVar -> 读取时版本) 与写集 (TVar -> 新值)。
- 提交时持有全局提交锁, 校验读集版本全部未变, 校验通过才整体应用写集;
  否则抛出 ConflictError, 写集全部丢弃 (整体回滚)。
- atomically() 捕获 ConflictError 后自动重试, 超过 max_retries 抛出
  RetryLimitExceeded。
- 嵌套事务: transaction() 在内层开启子事务, 子事务提交只是把读/写集合并
  进父事务; 子事务显式放弃 (abort) 只丢弃子事务写集, 不影响外层已有改动。
"""
from __future__ import annotations

import random
import threading
import time
from contextlib import contextmanager

__all__ = [
    "TVar",
    "Transaction",
    "transaction",
    "atomically",
    "ConflictError",
    "AbortTransaction",
    "RetryLimitExceeded",
    "get_stats",
    "reset_stats",
]


class ConflictError(Exception):
    """提交时读集校验失败, 事务已整体回滚。"""


class AbortTransaction(Exception):
    """显式放弃事务。顶层触发时不重试; 嵌套层触发时仅回滚嵌套部分。"""


class RetryLimitExceeded(Exception):
    """冲突重试次数耗尽。"""


# 提交临界区: 校验 + 应用写集必须串行, 保证校验-提交原子性。
_commit_lock = threading.Lock()

# 仅测试用: 强制接下来 N 次提交校验失败, 用于确定性构造冲突。
_force_conflicts = 0


class TVar:
    """事务变量。事务外只允许 peek() (无一致性保证, 用于初始化/断言)。"""

    __slots__ = ("_value", "_version")

    def __init__(self, value):
        self._value = value
        self._version = 0

    def peek(self):
        return self._value


class Transaction:
    """一次事务的读/写上下文。不要跨线程共享。"""

    __slots__ = ("parent", "read_set", "write_set")

    def __init__(self, parent: "Transaction | None" = None):
        self.parent = parent
        self.read_set = {}   # TVar -> 读取时版本
        self.write_set = {}  # TVar -> 待提交的新值

    def read(self, var: TVar):
        # 读己之写: 先看本事务写集, 再沿父链向上看。
        tx = self
        while tx is not None:
            if var in tx.write_set:
                return tx.write_set[var]
            tx = tx.parent
        if var not in self.read_set:
            # version-value-version 双重检查, 避免读到提交中途的值。
            while True:
                v1 = var._version
                value = var._value
                v2 = var._version
                if v1 == v2:
                    break
            self.read_set[var] = v1
            return value
        return var._value

    def write(self, var: TVar, value) -> None:
        self.write_set[var] = value

    def abort(self):
        """显式放弃当前 (嵌套) 事务。"""
        raise AbortTransaction()


@contextmanager
def transaction(tx: Transaction):
    """在 tx 内开启嵌套事务。

    正常退出: 嵌套读/写集合并进外层 (对外仍是一次原子提交)。
    AbortTransaction: 仅丢弃嵌套写集, 外层已有改动不受影响。
    """
    nested = Transaction(parent=tx)
    try:
        yield nested
    except AbortTransaction:
        pass  # 嵌套回滚: 丢弃 nested.write_set, 外层写集原样保留
    else:
        for var, ver in nested.read_set.items():
            # 保留外层已记录的更早版本, 校验更保守、更安全。
            if var not in tx.read_set:
                tx.read_set[var] = ver
        tx.write_set.update(nested.write_set)


def _validate_and_commit(tx: Transaction) -> None:
    """校验读集并整体应用写集; 冲突则整体回滚 (写集直接丢弃)。"""
    global _force_conflicts
    with _commit_lock:
        if _force_conflicts > 0:  # 测试钩子
            _force_conflicts -= 1
            raise ConflictError("forced conflict (test hook)")
        for var, ver in tx.read_set.items():
            if var._version != ver:
                raise ConflictError(
                    f"read-set validation failed on TVar(id={id(var):#x})"
                )
        for var, value in tx.write_set.items():
            var._value = value
            var._version += 1


class _Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.commits = 0            # 成功提交的事务数
            self.conflicts = 0          # 校验失败次数 (每次失败即整体回滚)
            self.retries = 0            # 实际执行的重试次数
            self.aborts = 0             # 顶层显式放弃次数
            self.commit_latencies = []  # 每个成功事务从首次尝试到提交的耗时(秒)

    def snapshot(self):
        with self._lock:
            attempts = self.commits + self.conflicts
            lat = sorted(self.commit_latencies)
            p99 = lat[int(len(lat) * 0.99)] if lat else 0.0
            avg = sum(lat) / len(lat) if lat else 0.0
            return {
                "commits": self.commits,
                "conflicts": self.conflicts,
                "retries": self.retries,
                "aborts": self.aborts,
                "attempts": attempts,
                "conflict_rate": self.conflicts / attempts if attempts else 0.0,
                "avg_retries_per_commit": (
                    self.retries / self.commits if self.commits else 0.0
                ),
                "commit_latency_avg_s": avg,
                "commit_latency_p99_s": p99,
            }


_stats = _Stats()


def get_stats():
    return _stats.snapshot()


def reset_stats():
    _stats.reset()


def atomically(fn, max_retries: int = 100):
    """以事务方式执行 fn(tx), 冲突自动重试, 返回 fn 的返回值。

    - ConflictError: 整体回滚后重试, 带指数退避 + 抖动。
    - AbortTransaction: 显式放弃, 不重试, 直接向上抛出。
    - 重试超过 max_retries 次抛出 RetryLimitExceeded。
    """
    retries = 0
    started = time.perf_counter()
    while True:
        tx = Transaction()
        try:
            result = fn(tx)
            _validate_and_commit(tx)
        except AbortTransaction:
            with _stats._lock:
                _stats.aborts += 1
            raise
        except ConflictError:
            with _stats._lock:
                _stats.conflicts += 1
            if retries >= max_retries:
                raise RetryLimitExceeded(
                    f"transaction failed after {retries} retries"
                )
            retries += 1
            with _stats._lock:
                _stats.retries += 1
            backoff = min(0.0005 * (2 ** min(retries, 6)), 0.02)
            time.sleep(backoff * random.random())
        else:
            with _stats._lock:
                _stats.commits += 1
                _stats.commit_latencies.append(time.perf_counter() - started)
            return result


def _force_next_conflicts(n: int) -> None:
    """测试钩子: 强制接下来 n 次提交校验失败。"""
    global _force_conflicts
    with _commit_lock:
        _force_conflicts = n
