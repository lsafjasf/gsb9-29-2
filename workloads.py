"""负载定义与执行驱动。

两种执行模式：
- "real"         : 真实 threading 线程，用于测墙钟时间；
- "deterministic": 确定性轮转交错（每线程轮流执行一步），
                   同一负载多次运行结果逐字节一致，用于验证排名可复现。
"""
import threading
from collections import deque


def core_of(tid, num_cores):
    return tid % num_cores


# ---------- 负载生成器：每 yield 一次 = 执行了一步 ----------
def array_own_slot_ops(counter, n_slots, tid, num_cores, iterations, write_every=1):
    """负载A：每个线程反复写数组中自己的槽位（packed 时构成伪共享）。"""
    core = core_of(tid, num_cores)
    slot = tid % n_slots
    for i in range(iterations):
        if i % write_every == 0:
            counter.inc(slot, core, tid)
        else:
            counter._mem.tick()  # 与共享数据无关的本地工作
        yield


def global_counter_ops(counter, tid, num_cores, iterations, write_every=1):
    """负载B：所有线程写同一个全局计数器（真共享）。"""
    core = core_of(tid, num_cores)
    for i in range(iterations):
        if i % write_every == 0:
            counter.inc(core, tid)
        else:
            counter._mem.tick()
        yield


def sharded_counter_ops(counter, tid, num_cores, iterations, write_every=1):
    """负载B的缓解版：每线程写自己的分片。"""
    core = core_of(tid, num_cores)
    for i in range(iterations):
        if i % write_every == 0:
            counter.inc(tid, core)
        else:
            counter._mem.tick()
        yield


# ---------- 驱动 ----------
def run(workload_fns, mode):
    """workload_fns: list of generator functions（每个对应一个线程）。"""
    if mode == "real":
        threads = [threading.Thread(target=lambda g=g: deque(g(), maxlen=0))
                   for g in workload_fns]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    elif mode == "deterministic":
        active = [g() for g in workload_fns]
        while active:
            nxt = []
            for g in active:
                try:
                    next(g)
                    nxt.append(g)
                except StopIteration:
                    pass
            active = nxt
    else:
        raise ValueError(f"未知模式: {mode}")
