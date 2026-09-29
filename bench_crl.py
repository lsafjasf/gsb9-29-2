"""基准: 查询耗时 vs 列表规模。用法: python3 bench_crl.py"""

import random
import time
from datetime import datetime, timedelta, timezone

from crl_store import CRLStore

SIZES = [1_000, 10_000, 100_000, 1_000_000, 2_000_000]
QUERIES = 200_000  # 每个规模的查询次数(命中/未命中各半)

NOW = datetime.now(timezone.utc)


def bench(size: int) -> tuple:
    rng = random.Random(42)
    store = CRLStore(issuer="CN=Bench CA")

    t0 = time.perf_counter()
    store.load(
        range(size),
        this_update=NOW - timedelta(days=1),
        next_update=NOW + timedelta(days=7),
    )
    load_s = time.perf_counter() - t0

    # 一半命中(在列表内), 一半未命中(超出范围)
    probes = [rng.randrange(size) if i % 2 == 0 else size + i
              for i in range(QUERIES)]
    rng.shuffle(probes)

    store.is_revoked(0)  # 预热
    t0 = time.perf_counter()
    for s in probes:
        store.is_revoked(s)
    elapsed = time.perf_counter() - t0
    return load_s, elapsed / QUERIES * 1e6  # 加载秒数, 单次查询微秒


def main() -> None:
    print(f"{'列表规模':>12} {'加载耗时(s)':>12} {'单次查询(us)':>14} {'吞吐(万次/秒)':>14}")
    for size in SIZES:
        load_s, per_query_us = bench(size)
        print(f"{size:>12,} {load_s:>12.3f} {per_query_us:>14.3f} "
              f"{100 / per_query_us:>14.1f}")


if __name__ == "__main__":
    main()
