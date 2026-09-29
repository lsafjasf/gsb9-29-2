"""三种共享计数器布局：紧凑（易伪共享）、对齐填充、分片。

计数器的"值"保存在普通 Python 列表里保证正确性；
每次写入同时把对应的模拟地址上报给 MemoryModel 做缓存行记账。
"""
from memory_model import LINE_SIZE

COUNTER_SIZE = 8  # 每个计数器 8 字节（对标 C 的 int64）


class PackedCounterArray:
    """紧凑布局：n 个计数器连续存放，每行 8 个 -> 多线程写相邻槽位即伪共享。"""

    def __init__(self, mem, n, name="packed"):
        self._mem = mem
        self._values = [0] * n
        self._addrs = [mem.alloc(f"{name}[{i}]", COUNTER_SIZE) for i in range(n)]

    def inc(self, slot, core, tid):
        self._values[slot] += 1
        self._mem.write(core, self._addrs[slot], tid)

    def value(self, slot):
        return self._values[slot]

    def total(self):
        return sum(self._values)


class PaddedCounterArray:
    """对齐填充：每个计数器独占一个 64 字节缓存行，消除伪共享。"""

    def __init__(self, mem, n, name="padded"):
        self._mem = mem
        self._values = [0] * n
        self._addrs = [mem.alloc(f"{name}[{i}]", LINE_SIZE, align=LINE_SIZE)
                       for i in range(n)]

    def inc(self, slot, core, tid):
        self._values[slot] += 1
        self._mem.write(core, self._addrs[slot], tid)

    def value(self, slot):
        return self._values[slot]

    def total(self):
        return sum(self._values)


class ShardedCounter:
    """分片计数：每线程一个私有分片（各占一行），总和 = 各分片之和。

    用于缓解"所有线程写同一个计数器"的真共享。
    """

    def __init__(self, mem, n_threads, name="sharded"):
        self._mem = mem
        self._values = [0] * n_threads
        self._addrs = [mem.alloc(f"{name}.shard[{t}]", LINE_SIZE, align=LINE_SIZE)
                       for t in range(n_threads)]

    def inc(self, tid, core):
        self._values[tid] += 1
        self._mem.write(core, self._addrs[tid], tid)

    def total(self):
        return sum(self._values)


class SingleCounter:
    """单个全局计数器（真共享的对照组）。"""

    def __init__(self, mem, name="global"):
        self._mem = mem
        self._value = 0
        self._addr = mem.alloc(name, COUNTER_SIZE, align=LINE_SIZE)

    def inc(self, core, tid):
        self._value += 1
        self._mem.write(core, self._addr, tid)

    def total(self):
        return self._value
