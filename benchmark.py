"""查询代价基准：对比空间索引与暴力扫描。

输出每个维度下矩形范围查询与 kNN 的：
  candidates    候选数（叶子条目/点的几何判定次数）
  nodes_visited 访问树节点数
  idx_ms        索引查询耗时
  brute_ms      暴力扫描耗时
并给出高维退化的量化表现。

运行：python3 benchmark.py
"""

import random
import statistics

from spatial_index import IndexedStore


def rect_selectivity_side(dim, selectivity):
    return selectivity ** (1.0 / dim)


def run():
    rng = random.Random(4242)
    n = 10000
    queries = 40
    k = 10
    print(f"数据量 n={n}, 矩形查询 {queries} 次 (选择性 ~1%), kNN k={k}, {queries} 次\n")

    header = (
        f"{'dim':>4} | {'查询':>6} | {'候选数(均值)':>12} | {'访问节点(均值)':>14} | "
        f"{'索引ms':>9} | {'暴力ms':>9} | {'索引/暴力':>9}"
    )
    print(header)
    print("-" * len(header))

    rows = []
    for dim in (2, 4, 8, 16):
        store = IndexedStore(dim=dim)
        points = []
        for i in range(n):
            point = tuple(rng.random() for _ in range(dim))
            store.insert(i, key=i, point=point)
            points.append(point)

        # ---- 矩形 ----
        side = rect_selectivity_side(dim, 0.01)
        cand, nodes, idx_t, brute_t = [], [], [], []
        for _ in range(queries):
            lo = tuple(rng.uniform(0.0, 1.0 - side) for _ in range(dim))
            hi = tuple(x + side for x in lo)
            rids, stats = store.query_rect(lo, hi)
            want = store.brute_rect(lo, hi)
            assert sorted(rids) == want, "矩形对拍失败"
            cand.append(stats.candidates)
            nodes.append(stats.nodes_visited)
            idx_t.append(stats.elapsed * 1000)
            brute_t.append(_time_brute_rect(points, lo, hi) * 1000)

        ratio = statistics.mean(idx_t) / statistics.mean(brute_t)
        rows.append((dim, "rect", cand, nodes, idx_t, brute_t, ratio))
        print(
            f"{dim:>4} | {'矩形':>6} | {statistics.mean(cand):>12.1f} | "
            f"{statistics.mean(nodes):>14.1f} | {statistics.mean(idx_t):>9.3f} | "
            f"{statistics.mean(brute_t):>9.3f} | {ratio:>9.2f}x"
        )

        # ---- kNN ----
        cand, nodes, idx_t, brute_t = [], [], [], []
        for _ in range(queries):
            q = tuple(rng.random() for _ in range(dim))
            rids, stats = store.query_knn(q, k)
            want = store.brute_knn(q, k)
            assert [store.distance(r, q) for r in rids] == [
                store.distance(r, q) for r in want
            ], "kNN 对拍失败"
            cand.append(stats.candidates)
            nodes.append(stats.nodes_visited)
            idx_t.append(stats.elapsed * 1000)
            brute_t.append(_time_brute_knn(points, q, k) * 1000)

        ratio = statistics.mean(idx_t) / statistics.mean(brute_t)
        rows.append((dim, "knn", cand, nodes, idx_t, brute_t, ratio))
        print(
            f"{dim:>4} | {'kNN':>6} | {statistics.mean(cand):>12.1f} | "
            f"{statistics.mean(nodes):>14.1f} | {statistics.mean(idx_t):>9.3f} | "
            f"{statistics.mean(brute_t):>9.3f} | {ratio:>9.2f}x"
        )

    print("\n退化说明（相对 2 维基线）：")
    base = {(d, q): (statistics.mean(c), statistics.mean(v))
            for d, q, c, v, *_ in rows if d == 2}
    for dim, q, c, v, *_ in rows:
        if dim == 2:
            continue
        b = base[(2, q)]
        print(
            f"  dim={dim:>2} {q:>4}: 候选数 {statistics.mean(c):8.1f} "
            f"(x{statistics.mean(c) / b[0]:5.1f}), "
            f"访问节点 {statistics.mean(v):7.1f} (x{statistics.mean(v) / b[1]:5.1f})"
        )
    print(
        "\n维度升高后节点 MBR 在各轴上互相重叠，矩形查询几乎剪不掉任何分支，\n"
        "候选数/访问节点数趋向全量 n，索引退化为线性扫描；同时每次距离/相交\n"
        "判定本身还要乘上维度系数 O(d)，这就是 R-tree 在高维的“维度灾难”。"
    )

    # ---- 退化极端场景：全部同位置 ----
    print("\n极端场景：n=5000 个点全部位于同一位置（2 维）")
    same = IndexedStore(dim=2)
    for i in range(5000):
        same.insert(i, key=i, point=(0.5, 0.5))
    rids, stats = same.query_rect((0.5, 0.5), (0.5, 0.5))
    assert len(rids) == 5000
    print(
        f"  零面积矩形命中全部: 候选数={stats.candidates}, 访问节点={stats.nodes_visited}, "
        f"耗时={stats.elapsed * 1000:.3f} ms（所有叶子都无法剪枝）"
    )
    rids, stats = same.query_knn((0.5, 0.5), 10)
    print(
        f"  kNN(k=10, 全部平局): 候选数={stats.candidates}, 访问节点={stats.nodes_visited}, "
        f"耗时={stats.elapsed * 1000:.3f} ms（平局时仅需 k 次出队）"
    )


def _time_brute_rect(points, lo, hi):
    import time
    start = time.perf_counter()
    n = 0
    for p in points:
        if all(a <= x <= b for a, b, x in zip(lo, hi, p)):
            n += 1
    return time.perf_counter() - start + 1e-12


def _time_brute_knn(points, q, k):
    import time
    start = time.perf_counter()
    sorted(
        range(len(points)),
        key=lambda i: sum((a - b) ** 2 for a, b in zip(points[i], q)),
    )[:k]
    return time.perf_counter() - start + 1e-12


if __name__ == "__main__":
    run()
