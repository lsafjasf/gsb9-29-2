"""Query-cost benchmark: candidates, nodes visited, elapsed time,
plus high-dimensional degradation of the R-tree.

Run:  python3 bench_cost.py
"""

import random
import time

from spatial_index import SpatialDB


def brute_rect(records, mins, maxs):
    return [k for k, (p, _) in records.items()
            if all(lo <= x <= hi for lo, hi, x in zip(mins, maxs, p))]


def brute_knn(records, q, k):
    return sorted(records,
                  key=lambda key: (sum((a - b) ** 2 for a, b in
                                       zip(records[key][0], q)), key))[:k]


def bench_2d():
    print("== 2D workload: n=20000 uniform points in unit square ==")
    random.seed(2024)
    n = 20000
    db = SpatialDB(dim=2, max_entries=16)
    records = {}
    for i in range(n):
        p = (random.random(), random.random())
        db.insert(i, p)
        records[i] = (p, None)
    db.assert_consistent()
    total_nodes = db.node_count()
    print("index nodes: %d\n" % total_nodes)

    print("-- rectangle range query (index vs brute force) --")
    print("%10s %8s %8s %10s %10s | %10s %8s" %
          ("selectiv.", "results", "cands", "nodes", "idx_ms", "brute_ms", "speedup"))
    for sel in (0.0001, 0.001, 0.01, 0.1):
        side = sel ** 0.5
        agg_c = agg_n = 0
        agg_t = agg_bt = 0.0
        rounds = 30
        for _ in range(rounds):
            x = random.uniform(0, 1 - side)
            y = random.uniform(0, 1 - side)
            got, st = db.rect_query((x, y), (x + side, y + side))
            t0 = time.perf_counter_ns()
            want = brute_rect(records, (x, y), (x + side, y + side))
            agg_bt += time.perf_counter_ns() - t0
            assert set(got) == set(want)
            agg_c += st.candidates
            agg_n += st.nodes_visited
            agg_t += st.elapsed_ns
        idx_ms = agg_t / rounds / 1e6
        brute_ms = agg_bt / rounds / 1e6
        print("%10.4f %8d %8d %10d %10.3f | %10.3f %7.1fx" %
              (sel, len(got), agg_c // rounds, agg_n // rounds,
               idx_ms, brute_ms, brute_ms / max(idx_ms, 1e-9)))

    print("\n-- kNN query (index vs brute force) --")
    print("%6s %8s %8s %10s | %10s %8s" %
          ("k", "cands", "nodes", "idx_ms", "brute_ms", "speedup"))
    for k in (1, 10, 100):
        agg_c = agg_n = 0
        agg_t = agg_bt = 0.0
        rounds = 30
        for _ in range(rounds):
            q = (random.random(), random.random())
            got, st = db.knn(q, k)
            t0 = time.perf_counter_ns()
            want = brute_knn(records, q, k)
            agg_bt += time.perf_counter_ns() - t0
            assert got == want
            agg_c += st.candidates
            agg_n += st.nodes_visited
            agg_t += st.elapsed_ns
        idx_ms = agg_t / rounds / 1e6
        brute_ms = agg_bt / rounds / 1e6
        print("%6d %8d %8d %10.3f | %10.3f %7.1fx" %
              (k, agg_c // rounds, agg_n // rounds, idx_ms, brute_ms,
               brute_ms / max(idx_ms, 1e-9)))


def bench_high_dim():
    print("\n== high-dimensional degradation: n=5000, rect selectivity=1% ==")
    print("%5s %8s %8s %10s %10s %10s | %10s %8s" %
          ("dim", "nodes", "cands", "cand/n", "nodes/all", "idx_ms", "brute_ms", "speedup"))
    for dim in (2, 4, 8, 16, 32):
        random.seed(1000 + dim)
        n = 5000
        db = SpatialDB(dim=dim, max_entries=16)
        records = {}
        for i in range(n):
            p = tuple(random.random() for _ in range(dim))
            db.insert(i, p)
            records[i] = (p, None)
        db.assert_consistent()
        total_nodes = db.node_count()
        side = 0.01 ** (1.0 / dim)
        agg_c = agg_n = 0
        agg_t = agg_bt = 0.0
        rounds = 20
        for _ in range(rounds):
            lo = tuple(random.uniform(0, 1 - side) for _ in range(dim))
            hi = tuple(x + side for x in lo)
            got, st = db.rect_query(lo, hi)
            t0 = time.perf_counter_ns()
            want = brute_rect(records, lo, hi)
            agg_bt += time.perf_counter_ns() - t0
            assert set(got) == set(want)
            agg_c += st.candidates
            agg_n += st.nodes_visited
            agg_t += st.elapsed_ns
        idx_ms = agg_t / rounds / 1e6
        brute_ms = agg_bt / rounds / 1e6
        print("%5d %8d %8d %10.3f %10.3f %10.3f | %10.3f %7.1fx" %
              (dim, total_nodes, agg_c // rounds, agg_c / rounds / n,
               agg_n / rounds / total_nodes, idx_ms, brute_ms,
               brute_ms / max(idx_ms, 1e-9)))
    print("""
观察: 维度升高时, 各节点 MBR 在高维空间中相互重叠加剧,
矩形查询几乎要与所有节点的 MBR 相交 -> 访问节点数/总节点数 -> 1,
候选数 -> n, 索引退化为暴力扫描(还多付 MBR 维护开销)。
这就是 R-tree 类索引的"维度诅咒": 经验上 dim >~ 10 后
剪枝收益基本消失, 应考虑降维或改用扫描+过滤。""")


if __name__ == "__main__":
    bench_2d()
    bench_high_dim()
