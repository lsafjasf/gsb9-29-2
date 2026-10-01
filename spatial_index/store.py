"""IndexedStore：键索引 + 空间索引的组合存储。

- 每条记录：rid -> (key, point)
- 键查询（等值 / 范围）走 KeyIndex
- 空间查询（矩形范围 / kNN）走 RTree
- 所有更新（插入、删除、位置变更、改键）同步维护两个索引
- assert_consistent() 校验索引与数据完全一致
"""

from __future__ import annotations

import math

from .keyindex import KeyIndex
from .rtree import RTree


class IndexedStore:
    def __init__(self, dim, max_entries=8):
        self.dim = dim
        self._records = {}  # rid -> (key, point)
        self._keys = KeyIndex()
        self._tree = RTree(dim, max_entries=max_entries)

    def __len__(self):
        return len(self._records)

    def __contains__(self, rid):
        return rid in self._records

    # ---------- 更新 ----------

    def insert(self, rid, key, point):
        if rid in self._records:
            raise ValueError(f"duplicate rid: {rid!r}")
        point = tuple(point)
        if len(point) != self.dim:
            raise ValueError("point dimension mismatch")
        self._records[rid] = (key, point)
        self._keys.insert(key, rid)
        self._tree.insert(rid, point)

    def delete(self, rid):
        key, point = self._records.pop(rid)  # 不存在则 KeyError
        self._keys.delete(key, rid)
        self._tree.delete(rid, point)

    def move(self, rid, new_point):
        """位置变更，索引同步更新。"""
        key, old_point = self._records[rid]
        new_point = tuple(new_point)
        if len(new_point) != self.dim:
            raise ValueError("point dimension mismatch")
        self._tree.move(rid, old_point, new_point)
        self._records[rid] = (key, new_point)

    def set_key(self, rid, new_key):
        old_key, point = self._records[rid]
        self._keys.delete(old_key, rid)
        self._keys.insert(new_key, rid)
        self._records[rid] = (new_key, point)

    # ---------- 读取 ----------

    def key_of(self, rid):
        return self._records[rid][0]

    def position(self, rid):
        return self._records[rid][1]

    def distance(self, rid, point):
        return math.dist(self._records[rid][1], tuple(point))

    # ---------- 索引查询 ----------

    def query_key_eq(self, key):
        rids, stats = self._keys.query_eq(key)
        return sorted(rids), stats

    def query_key_range(self, lo, hi):
        rids, stats = self._keys.query_range(lo, hi)
        return sorted(rids), stats

    def query_rect(self, lo, hi):
        rids, stats = self._tree.range_query(lo, hi)
        return sorted(rids), stats

    def query_knn(self, point, k):
        return self._tree.knn(point, k)

    # ---------- 暴力扫描（对拍基准） ----------

    def brute_key_eq(self, key):
        return sorted(rid for rid, (k, _p) in self._records.items() if k == key)

    def brute_key_range(self, lo, hi):
        return sorted(
            rid for rid, (k, _p) in self._records.items() if lo <= k <= hi
        )

    def brute_rect(self, lo, hi):
        lo, hi = tuple(lo), tuple(hi)
        return sorted(
            rid
            for rid, (_k, p) in self._records.items()
            if all(a <= x <= b for a, b, x in zip(lo, hi, p))
        )

    def brute_knn(self, point, k):
        point = tuple(point)
        ranked = sorted(
            self._records,
            key=lambda rid: (math.dist(self._records[rid][1], point), str(rid)),
        )
        return ranked[:k]

    # ---------- 一致性断言 ----------

    def assert_consistent(self):
        """校验两个索引与记录集完全一致，失败抛 AssertionError。"""
        # 1) R-tree 结构合法（父指针 / MBR / 容量 / 条目数）
        self._tree.validate()

        # 2) R-tree 内容 == 记录集（rid 集合与位置都一致）
        tree_points = dict(self._tree.items())
        assert set(tree_points) == set(self._records), (
            "R-tree rid set diverged from records"
        )
        for rid, (_key, point) in self._records.items():
            assert tree_points[rid] == point, f"stale position for {rid!r}"

        # 3) 键索引内容 == 记录集
        expected = sorted((key, rid) for rid, (key, _p) in self._records.items())
        assert self._keys.items() == expected, "key index diverged from records"

        # 4) 查询层面对拍：全空间矩形 == 全量；抽样点矩形 / kNN == 暴力
        if self._records:
            lo = tuple(min(p[d] for _k, p in self._records.values()) for d in range(self.dim))
            hi = tuple(max(p[d] for _k, p in self._records.values()) for d in range(self.dim))
            rids, _stats = self.query_rect(lo, hi)
            assert set(rids) == set(self._records), "full-space rect query mismatch"

            sample = sorted(self._records)[:16]
            for rid in sample:
                point = self._records[rid][1]
                got, _ = self.query_rect(point, point)
                assert rid in got, f"point rect query lost {rid!r}"
                knn, _ = self.query_knn(point, 1)
                assert knn and self.distance(knn[0], point) == 0.0, (
                    f"knn self-query failed for {rid!r}"
                )
                key = self._records[rid][0]
                assert rid in self.query_key_eq(key)[0], f"key eq lost {rid!r}"
