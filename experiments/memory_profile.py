#!/usr/bin/env python3
"""内存与耗时剖析：random.choices 的空间/时间不变量。

被测对象: CPython 3.12.3 标准库 random.Random.choices
          (/usr/lib/python3.12/random.py:454)
运行方式: python3 experiments/memory_profile.py
仅使用标准库；固定种子 SEED=20260930，结果可完整复现。

要点:
  L469  cum_weights = list(_accumulate(weights))  # 每次调用都重新分配 O(n) 列表
  L488  返回长度为 k 的结果列表                    # O(k) 额外内存
  无缓存、无状态：两次调用之间不保留任何与 n 相关的内存。
"""

import gc
import random
import time
import tracemalloc

SEED = 20260930
N = 1_000_000


def measure_peak(fn):
    gc.collect()
    tracemalloc.start()
    before, _ = tracemalloc.get_traced_memory()
    fn()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak - before


def main():
    rng = random.Random(SEED)
    pop = list(range(N))

    print(f"总体规模 n = {N:,}，种子 = {SEED}\n")

    # M1: 整数权重 -> cum_weights 为 int 列表（PyLong，每个约 28-32 字节 + 8 字节指针）
    w_int = [1] * N
    peak = measure_peak(lambda: rng.choices(pop, weights=w_int, k=1))
    print(f"M1 整数权重 k=1   : 峰值额外内存 {peak / 2**20:8.1f} MiB"
          f"（cum_weights 列表，O(n)，约 {peak / N:.0f} 字节/元素）")

    # M2: 浮点权重 -> cum_weights 为 float 列表
    w_float = [1.0] * N
    peak = measure_peak(lambda: rng.choices(pop, weights=w_float, k=1))
    print(f"M2 浮点权重 k=1   : 峰值额外内存 {peak / 2**20:8.1f} MiB"
          f"（同上，float 对象略小于大 int）")

    # M3: 结果列表 O(k)
    k = 1_000_000
    peak = measure_peak(lambda: rng.choices(pop[:100], weights=[1] * 100, k=k))
    print(f"M3 n=100, k={k:,}: 峰值额外内存 {peak / 2**20:8.1f} MiB"
          f"（结果列表 O(k)，约 {peak / k:.0f} 字节/结果）")

    # M4: 无状态验证 —— 调用结束后不保留与 n 相关的内存
    gc.collect()
    tracemalloc.start()
    base, _ = tracemalloc.get_traced_memory()
    rng.choices(pop, weights=w_int, k=1)
    gc.collect()
    after, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"M4 调用后残留内存 : {(after - base) / 2**10:8.1f} KiB"
          f"（≈0 -> 无缓存、无跨调用状态）")

    # M5: 每次调用都重新 O(n) 累加 —— 无权重缓存
    reps = 20
    t0 = time.perf_counter()
    for _ in range(reps):
        rng.choices(pop, weights=w_int, k=1)
    t_recompute = (time.perf_counter() - t0) / reps

    cum_precomputed = list(range(1, N + 1))  # 调用方自行缓存的前缀和
    t0 = time.perf_counter()
    for _ in range(reps):
        rng.choices(pop, cum_weights=cum_precomputed, k=1)
    t_cum = (time.perf_counter() - t0) / reps

    t0 = time.perf_counter()
    rng.choices(pop, weights=w_int, k=N)
    t_k = time.perf_counter() - t0

    print(f"\nM5 耗时（n={N:,}）:")
    print(f"  weights 路径 k=1     : {t_recompute * 1e3:8.1f} ms/次（每次重新 O(n) 累加）")
    print(f"  cum_weights 路径 k=1 : {t_cum * 1e6:8.1f} µs/次（跳过累加，仅校验+二分）")
    print(f"  weights 路径 k=n     : {t_k * 1e3:8.1f} ms/次（累加 O(n) + 抽取 O(k log n)）")
    print("  -> 同一批权重反复调用时，前缀和被反复重算；"
          "调用方若自行缓存 cum_weights 可省去该开销。")


if __name__ == "__main__":
    main()
