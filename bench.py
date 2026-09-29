"""基准：查询耗时与列表规模的关系（哈希索引 vs 线性扫描）。

运行：python3 bench.py
"""
import random
import time
from datetime import datetime, timedelta, timezone

from crl_index import CRLIndex

UTC = timezone.utc
NOW = datetime.now(UTC)
ISSUER = "CN=Bench CA"

SIZES = [1_000, 10_000, 100_000, 500_000, 1_000_000, 2_000_000]
QUERIES = 20_000  # 每种规模下的查询次数（命中/未命中各半）


def bench_index(serials, probes):
    idx = CRLIndex(ISSUER)
    t0 = time.perf_counter()
    idx.load(serials, this_update=NOW - timedelta(hours=1), next_update=NOW + timedelta(hours=24))
    load_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    for s in probes:
        idx.is_revoked(s, now=NOW)
    query_s = time.perf_counter() - t0
    return load_s, query_s / len(probes)


def bench_linear(serials_list, probes):
    t0 = time.perf_counter()
    for s in probes:
        for v in serials_list:  # 模拟无索引的线性扫描
            if v == s:
                break
    return (time.perf_counter() - t0) / len(probes)


def main():
    print(f"{'规模':>10} | {'加载(s)':>8} | {'索引查询(us/次)':>14} | {'线性扫描(us/次)':>16} | {'加速比':>8}")
    print("-" * 70)
    for n in SIZES:
        serials = random.sample(range(n * 4), n)  # 稀疏序列号空间，更接近真实
        probes = [random.choice(serials) if i % 2 == 0 else random.randrange(n * 4, n * 8)
                  for i in range(QUERIES)]
        load_s, idx_q = bench_index(serials, probes)
        # 线性扫描在超大列表上过慢，只做少量探测后按线性外推
        lin_probes = probes if n <= 100_000 else probes[:200]
        lin_q = bench_linear(serials, lin_probes)
        print(f"{n:>10,} | {load_s:>8.3f} | {idx_q*1e6:>14.3f} | {lin_q*1e6:>16.1f} | {lin_q/idx_q:>7.0f}x")


if __name__ == "__main__":
    random.seed(42)
    main()
