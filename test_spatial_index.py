"""自测：边界用例 + 与暴力扫描的随机对拍。

运行：python3 -m unittest test_spatial_index -v
"""

import math
import random
import unittest

from spatial_index import IndexedStore


def brute_rect(records, lo, hi):
    return sorted(
        rid
        for rid, (_k, p) in records.items()
        if all(a <= x <= b for a, b, x in zip(lo, hi, p))
    )


def brute_knn_dists(records, point, k):
    dists = sorted(math.dist(p, point) for _k, p in records.values())
    return dists[:k]


def brute_key_eq(records, key):
    return sorted(rid for rid, (k, _p) in records.items() if k == key)


def brute_key_range(records, lo, hi):
    return sorted(rid for rid, (k, _p) in records.items() if lo <= k <= hi)


class TestEdgeCases(unittest.TestCase):
    def test_empty_index(self):
        store = IndexedStore(dim=2)
        self.assertEqual(len(store), 0)
        self.assertEqual(store.query_key_eq("a")[0], [])
        self.assertEqual(store.query_key_range(0, 100)[0], [])
        rids, stats = store.query_rect((0, 0), (1, 1))
        self.assertEqual(rids, [])
        self.assertEqual(stats.candidates, 0)
        rids, stats = store.query_knn((0, 0), 5)
        self.assertEqual(rids, [])
        self.assertEqual(stats.candidates, 0)
        store.assert_consistent()

    def test_single_point(self):
        store = IndexedStore(dim=2)
        store.insert("p1", key=10, point=(1.0, 2.0))
        hit, _ = store.query_rect((0, 0), (1.5, 2.5))
        self.assertEqual(hit, ["p1"])
        edge, _ = store.query_rect((1.0, 2.0), (1.0, 2.0))  # 边界闭区间
        self.assertEqual(edge, ["p1"])
        miss, _ = store.query_rect((3, 3), (4, 4))
        self.assertEqual(miss, [])
        knn, _ = store.query_knn((0, 0), 5)  # k > n
        self.assertEqual(knn, ["p1"])
        self.assertEqual(store.query_key_eq(10)[0], ["p1"])
        self.assertEqual(store.query_key_range(0, 100)[0], ["p1"])
        self.assertEqual(store.query_key_eq(11)[0], [])
        store.assert_consistent()
        store.delete("p1")
        self.assertEqual(len(store), 0)
        store.assert_consistent()

    def test_all_same_position(self):
        store = IndexedStore(dim=3)
        n = 200
        for i in range(n):
            store.insert(i, key=i % 7, point=(5.0, 5.0, 5.0))
        store.assert_consistent()
        # 零面积矩形命中全部
        rids, _ = store.query_rect((5, 5, 5), (5, 5, 5))
        self.assertEqual(set(rids), set(range(n)))
        # 偏离一点的矩形不命中
        rids, _ = store.query_rect((5.1, 5, 5), (6, 6, 6))
        self.assertEqual(rids, [])
        # kNN：距离全相等，数量正确即可
        knn, _ = store.query_knn((5.0, 5.0, 5.0), 10)
        self.assertEqual(len(knn), 10)
        self.assertTrue(all(store.distance(r, (5, 5, 5)) == 0.0 for r in knn))
        knn, _ = store.query_knn((0.0, 0.0, 0.0), n + 50)  # k > n
        self.assertEqual(len(knn), n)
        # 键等值
        self.assertEqual(len(store.query_key_eq(3)[0]), len([i for i in range(n) if i % 7 == 3]))
        # 同位置下删除一半仍一致
        for i in range(0, n, 2):
            store.delete(i)
        store.assert_consistent()
        rids, _ = store.query_rect((5, 5, 5), (5, 5, 5))
        self.assertEqual(set(rids), set(range(1, n, 2)))

    def test_frequent_moves(self):
        rng = random.Random(20261001)
        store = IndexedStore(dim=2)
        mirror = {}
        n = 300
        for i in range(n):
            point = (rng.uniform(0, 100), rng.uniform(0, 100))
            store.insert(i, key=rng.randrange(50), point=point)
            mirror[i] = point
        # 高频移动：每轮动 30 个点，动完立刻对拍
        for _round in range(40):
            for _ in range(30):
                rid = rng.randrange(n)
                point = (rng.uniform(0, 100), rng.uniform(0, 100))
                store.move(rid, point)
                mirror[rid] = point
            lo = (rng.uniform(0, 60), rng.uniform(0, 60))
            hi = (lo[0] + rng.uniform(0, 40), lo[1] + rng.uniform(0, 40))
            got, _ = store.query_rect(lo, hi)
            want = sorted(
                rid for rid, p in mirror.items()
                if lo[0] <= p[0] <= hi[0] and lo[1] <= p[1] <= hi[1]
            )
            self.assertEqual(got, want)
            q = (rng.uniform(0, 100), rng.uniform(0, 100))
            knn, _ = store.query_knn(q, 7)
            want_dists = sorted(math.dist(p, q) for p in mirror.values())[:7]
            self.assertEqual(
                sorted(store.distance(r, q) for r in knn), want_dists
            )
        store.assert_consistent()

    def test_delete_all_leaves_empty_index(self):
        rng = random.Random(7)
        store = IndexedStore(dim=4)
        n = 500
        for i in range(n):
            store.insert(i, key=i, point=tuple(rng.random() for _ in range(4)))
        store.assert_consistent()
        order = list(range(n))
        rng.shuffle(order)
        for rid in order:
            store.delete(rid)
        self.assertEqual(len(store), 0)
        rids, stats = store.query_rect((0, 0, 0, 0), (1, 1, 1, 1))
        self.assertEqual(rids, [])
        self.assertEqual(stats.candidates, 0)
        self.assertEqual(store.query_knn((0, 0, 0, 0), 3)[0], [])
        store.assert_consistent()
        # 删空后还能继续用
        store.insert("back", key=1, point=(0.5, 0.5, 0.5, 0.5))
        self.assertEqual(store.query_rect((0, 0, 0, 0), (1, 1, 1, 1))[0], ["back"])
        store.assert_consistent()


class TestDifferential(unittest.TestCase):
    """随机操作序列下，索引查询结果必须与暴力扫描完全一致。"""

    def test_random_ops_match_brute_force(self):
        rng = random.Random(991)
        dim = 3
        store = IndexedStore(dim=dim)
        mirror = {}  # rid -> (key, point)
        next_rid = 0

        def rand_point():
            return tuple(rng.uniform(-100, 100) for _ in range(dim))

        def check_queries():
            # 矩形范围对拍
            corner = rand_point()
            side = [rng.uniform(0, 80) for _ in range(dim)]
            lo = corner
            hi = tuple(c + s for c, s in zip(corner, side))
            got, _ = store.query_rect(lo, hi)
            want = brute_rect(mirror, lo, hi)
            self.assertEqual(got, want)
            # kNN 对拍（距离序列一致，平局不纠结顺序）
            q = rand_point()
            k = rng.randrange(1, 12)
            knn, _ = store.query_knn(q, k)
            self.assertEqual(
                sorted(store.distance(r, q) for r in knn),
                brute_knn_dists(mirror, q, k),
            )
            self.assertEqual(len(knn), min(k, len(mirror)))
            # 键等值 / 范围对拍
            key = rng.randrange(200)
            self.assertEqual(store.query_key_eq(key)[0], brute_key_eq(mirror, key))
            klo = rng.randrange(200)
            khi = klo + rng.randrange(60)
            self.assertEqual(
                store.query_key_range(klo, khi)[0], brute_key_range(mirror, klo, khi)
            )

        for step in range(2000):
            op = rng.random()
            if op < 0.40 or not mirror:  # 插入
                rid = next_rid
                next_rid += 1
                key = rng.randrange(200)
                point = rand_point()
                store.insert(rid, key=key, point=point)
                mirror[rid] = (key, point)
            elif op < 0.60:  # 删除
                rid = rng.choice(list(mirror))
                store.delete(rid)
                del mirror[rid]
            elif op < 0.85:  # 位置变更
                rid = rng.choice(list(mirror))
                point = rand_point()
                store.move(rid, point)
                mirror[rid] = (mirror[rid][0], point)
            else:  # 改键
                rid = rng.choice(list(mirror))
                key = rng.randrange(200)
                store.set_key(rid, key)
                mirror[rid] = (key, mirror[rid][1])
            if step % 20 == 0:
                check_queries()
            if step % 200 == 0:
                store.assert_consistent()
        store.assert_consistent()
        check_queries()

    def test_high_dim_differential(self):
        rng = random.Random(31337)
        dim = 10
        store = IndexedStore(dim=dim)
        mirror = {}
        for i in range(800):
            point = tuple(rng.random() for _ in range(dim))
            store.insert(i, key=i, point=point)
            mirror[i] = (i, point)
        for _ in range(50):
            lo = tuple(rng.random() * 0.5 for _ in range(dim))
            hi = tuple(l + rng.random() * 0.5 for l in lo)
            got, _ = store.query_rect(lo, hi)
            self.assertEqual(got, brute_rect(mirror, lo, hi))
            q = tuple(rng.random() for _ in range(dim))
            knn, _ = store.query_knn(q, 5)
            self.assertEqual(
                sorted(store.distance(r, q) for r in knn),
                brute_knn_dists(mirror, q, 5),
            )
        store.assert_consistent()


if __name__ == "__main__":
    unittest.main()
