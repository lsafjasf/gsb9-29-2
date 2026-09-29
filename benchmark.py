"""争用检测与缓解效果对比基准。

用法:
    python3 benchmark.py [--runs 5] [--cores 4] [--top 5]

对每个场景：
  负载A（各写各的槽位）: PackedCounterArray vs PaddedCounterArray  -> 验证对齐填充
  负载B（共写一个计数器）: SingleCounter vs ShardedCounter          -> 验证分片计数
每个组合用真实线程测墙钟，用确定性交错验证排名可复现。
"""
import argparse
import time
from dataclasses import dataclass

from memory_model import MemoryModel
from counters import (PackedCounterArray, PaddedCounterArray,
                      SingleCounter, ShardedCounter)
from contention import analyze, ranking_signature, format_report
import workloads


@dataclass
class Result:
    impl: str
    writes: int
    transfers: int
    write_cycles: int   # 确定性交错下，写操作消耗的周期（可复现）
    wall_s: float       # 真实线程墙钟
    reports: list

    @property
    def avg_latency(self):
        return self.write_cycles / self.writes if self.writes else 0.0

    @property
    def sim_throughput(self):  # 次/秒（按模拟周期折算）
        return self.writes / (self.write_cycles / 3_000_000_000) \
            if self.write_cycles else 0.0

    @property
    def wall_throughput(self):
        return self.writes / self.wall_s if self.wall_s else 0.0

    @property
    def sim_time_us(self):  # 模拟耗时（微秒），3GHz 折算
        return self.write_cycles / 3000.0


def measure(name, build, n_threads, cores, iterations, write_every, mode):
    """mode="deterministic": 只跑确定性交错，统计可复现；
    mode="real": 只跑真实线程；mode="both": 确定性出统计 + 真实线程出墙钟。"""
    def once(run_mode):
        mem = MemoryModel(cores)
        counter, op = build(mem)
        fns = [(lambda t=t: op(counter, t)) for t in range(n_threads)]
        start = time.perf_counter()
        workloads.run(fns, run_mode)
        wall = time.perf_counter() - start
        return mem, wall

    if mode == "both":
        mem, _ = once("deterministic")
        _, wall = once("real")
    else:
        mem, wall = once(mode)
        if mode == "deterministic":
            wall = 0.0
    return Result(name, mem.total_writes,
                  sum(s.transfers for s in mem.lines.values()),
                  mem.write_cycles, wall, analyze(mem))


def scenario_builders(n_threads, cores, iterations, write_every):
    """返回 [(场景, [(实现名, builder), ...]), ...]"""
    def array_op(counter, tid):
        return workloads.array_own_slot_ops(
            counter, n_threads, tid, cores, iterations, write_every)

    def global_op(counter, tid):
        return workloads.global_counter_ops(
            counter, tid, cores, iterations, write_every)

    def sharded_op(counter, tid):
        return workloads.sharded_counter_ops(
            counter, tid, cores, iterations, write_every)

    return [
        ("负载A: 每线程写自己的槽位(伪共享场景)", [
            ("紧凑数组", lambda m: (PackedCounterArray(m, n_threads), array_op)),
            ("对齐填充", lambda m: (PaddedCounterArray(m, n_threads), array_op)),
        ]),
        ("负载B: 所有线程写同一计数器(真共享场景)", [
            ("单计数器", lambda m: (SingleCounter(m), global_op)),
            ("分片计数", lambda m: (ShardedCounter(m, n_threads), sharded_op)),
        ]),
    ]


def fmt_row(r):
    return (f"{r.impl:<10}{r.writes:>10}{r.transfers:>10}"
            f"{r.avg_latency:>12.1f}{r.sim_throughput/1e6:>12.2f}"
            f"{r.sim_time_us:>12.1f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5, help="排名稳定性重复次数")
    ap.add_argument("--cores", type=int, default=4)
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args()

    scenarios = [
        ("场景1: 单线程", dict(n_threads=1, iterations=20000, write_every=1)),
        ("场景2: 线程数=核心数", dict(n_threads=args.cores, iterations=20000, write_every=1)),
        ("场景3: 线程数>>核心数(64线程)", dict(n_threads=64, iterations=5000, write_every=1)),
        ("场景4: 写入极稀疏(1/1000)", dict(n_threads=8, iterations=20000, write_every=1000)),
    ]

    print(f"配置: 核心数={args.cores}, 缓存行=64B, 行转移代价=100周期, 本地命中=1周期")
    print("说明: CPython 的 GIL 使真实墙钟被解释器调度主导、无法反映缓存行效应；")
    print("      吞吐/时延采用确定性缓存行模型，同一负载多次运行结果逐字节一致。\n")

    for sc_name, cfg in scenarios:
        print("=" * 96)
        print(f"{sc_name}  (线程={cfg['n_threads']}, 每线程迭代={cfg['iterations']}, "
              f"写间隔={cfg['write_every']})")
        print("=" * 96)
        for wl_name, impls in scenario_builders(cfg["n_threads"], args.cores,
                                                cfg["iterations"], cfg["write_every"]):
            print(f"\n-- {wl_name} --")
            print(f"{'实现':<10}{'写次数':>10}{'行转移':>10}{'均延迟(周期)':>12}"
                  f"{'模拟吞吐M/s':>12}{'模拟耗时us':>12}")
            results = []
            for impl_name, builder in impls:
                r = measure(impl_name, builder, cfg["n_threads"], args.cores,
                            cfg["iterations"], cfg["write_every"],
                            mode="deterministic")
                results.append(r)
                print(fmt_row(r))
            base, fixed = results
            if base.transfers:
                speedup = base.avg_latency / fixed.avg_latency if fixed.avg_latency else 0
                print(f"   -> 缓解后行转移 {base.transfers} -> {fixed.transfers}, "
                      f"平均延迟下降 {speedup:.1f}x")
            # 只对基线实现打印争用明细
            contended = [x for x in base.reports if x.kind != "private"]
            if contended:
                print(f"\n   争用明细(基线实现「{base.impl}」, top {args.top}):")
                print("   " + format_report(base.reports, top=args.top).replace("\n", "\n   "))
            else:
                print("   无争用：所有缓存行均为单线程私有。")

    # ---------- 排名稳定性 ----------
    print("\n" + "=" * 96)
    print(f"排名稳定性验证: 同负载重复 {args.runs} 次")
    print("=" * 96)
    for sc_name, cfg in scenarios[1:3]:  # 多线程密集写场景最有代表性
        for wl_name, impls in scenario_builders(cfg["n_threads"], args.cores,
                                                cfg["iterations"], cfg["write_every"]):
            impl_name, builder = impls[0]  # 基线实现
            for mode in ("deterministic", "real"):
                sigs = set()
                for _ in range(args.runs):
                    r = measure(impl_name, builder, cfg["n_threads"], args.cores,
                                cfg["iterations"], cfg["write_every"], mode=mode)
                    sigs.add(ranking_signature(r.reports))
                status = "稳定 ✓" if len(sigs) == 1 else f"不稳定 ✗ ({len(sigs)}种排名)"
                print(f"{sc_name} | {wl_name.split(':')[0]} | {impl_name} | "
                      f"{mode:<14}: {status}")


if __name__ == "__main__":
    main()
