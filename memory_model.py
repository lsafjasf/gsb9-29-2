"""缓存行内存模型：模拟 MESI 简化版的写所有权转移，按缓存行统计争用。

统计口径：
- 统计单元 = 64 字节缓存行（LINE_SIZE）。
- 每次 write() 记录：目标行、写线程、是否命中本地（行所有权已在当前核）。
- 行所有权在核之间易手记为一次 transfer（行转移），代价 LINE_TRANSFER_CYCLES 周期；
  本地命中代价 LOCAL_HIT_CYCLES 周期。这是主流多核 MESI 协议下
  "cache line ping-pong" 的标准近似模型。
"""
import threading
from dataclasses import dataclass, field

LINE_SIZE = 64                # 缓存行字节数
LOCAL_HIT_CYCLES = 1          # 行所有权在本核时的写代价
LINE_TRANSFER_CYCLES = 100    # 行跨核转移（所有权易手）的写代价
CPU_FREQ_HZ = 3_000_000_000   # 折算墙钟时间用的虚拟主频


@dataclass
class LineStats:
    writes: int = 0
    writers: set = field(default_factory=set)   # 写过该行的线程 id
    cold_misses: int = 0                        # 首次写（行尚未被任何核持有）
    transfers: int = 0                          # 行所有权跨核易手次数（争用核心指标）
    cycles: int = 0                             # 该行累计消耗的周期数


class MemoryModel:
    """线程安全的模拟地址空间 + 缓存行一致性记账器。"""

    def __init__(self, num_cores):
        if num_cores < 1:
            raise ValueError("num_cores 必须 >= 1")
        self.num_cores = num_cores
        self._owner = {}              # line_id -> 当前持有写所有权的核
        self.lines = {}               # line_id -> LineStats
        self._symbols = []            # (name, base, size)
        self._next_addr = 0
        self.total_cycles = 0
        self.write_cycles = 0
        self.total_writes = 0
        self._lock = threading.Lock()

    # ---------- 地址分配与符号 ----------
    def alloc(self, name, size, align=1):
        """分配 size 字节的地址空间并登记符号名，返回基址。"""
        if size <= 0:
            raise ValueError("size 必须为正")
        with self._lock:
            if align > 1:
                self._next_addr = -(-self._next_addr // align) * align
            base = self._next_addr
            self._next_addr += size
            self._symbols.append((name, base, size))
            return base

    def symbols_for_line(self, line_id):
        lo, hi = line_id * LINE_SIZE, (line_id + 1) * LINE_SIZE
        return [name for name, base, size in self._symbols
                if base < hi and base + size > lo]

    # ---------- 访存记账 ----------
    def write(self, core, addr, thread_id):
        """记录一次对 addr 的写。core = 执行写的核，thread_id = 线程标识。"""
        if not 0 <= core < self.num_cores:
            raise ValueError("非法 core id")
        line = addr // LINE_SIZE
        with self._lock:
            st = self.lines.setdefault(line, LineStats())
            st.writes += 1
            st.writers.add(thread_id)
            owner = self._owner.get(line)
            if owner is None:
                st.cold_misses += 1
                cost = LINE_TRANSFER_CYCLES
                self._owner[line] = core
            elif owner != core:
                st.transfers += 1
                cost = LINE_TRANSFER_CYCLES
                self._owner[line] = core
            else:
                cost = LOCAL_HIT_CYCLES
            st.cycles += cost
            self.total_cycles += cost
            self.write_cycles += cost
            self.total_writes += 1

    def tick(self, cost=LOCAL_HIT_CYCLES):
        """记录一次与共享行无关的本地工作（用于稀疏写负载的非写迭代）。"""
        with self._lock:
            self.total_cycles += cost

    # ---------- 汇总 ----------
    def simulated_seconds(self):
        return self.total_cycles / CPU_FREQ_HZ

    def reset(self):
        with self._lock:
            self._owner.clear()
            self.lines.clear()
            self.total_cycles = 0
            self.write_cycles = 0
            self.total_writes = 0
