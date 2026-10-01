"""有序键索引：等值 + 范围查询，基于 bisect 的有序数组，仅用标准库。"""

from __future__ import annotations

import bisect
import time

from .rtree import QueryStats


class KeyIndex:
    """按 (key, rid) 排序的有序数组。

    - 等值查询：二分定位 + 线性收集
    - 范围查询：二分定位下界 + 线性收集（闭区间）
    """

    def __init__(self):
        self._items = []  # 有序 (key, rid)

    def __len__(self):
        return len(self._items)

    def items(self):
        return list(self._items)

    def insert(self, key, rid):
        bisect.insort_right(self._items, (key, rid))

    def delete(self, key, rid):
        idx = bisect.bisect_left(self._items, (key, rid))
        if idx < len(self._items) and self._items[idx] == (key, rid):
            self._items.pop(idx)
        else:
            raise KeyError(rid)

    def query_eq(self, key):
        stats = QueryStats()
        start = time.perf_counter()
        idx = bisect.bisect_left(self._items, (key,))
        found = []
        while idx < len(self._items) and self._items[idx][0] == key:
            stats.candidates += 1
            found.append(self._items[idx][1])
            idx += 1
        stats.elapsed = time.perf_counter() - start
        return found, stats

    def query_range(self, lo, hi):
        """闭区间 [lo, hi]。"""
        stats = QueryStats()
        start = time.perf_counter()
        idx = bisect.bisect_left(self._items, (lo,))
        found = []
        while idx < len(self._items) and self._items[idx][0] <= hi:
            stats.candidates += 1
            found.append(self._items[idx][1])
            idx += 1
        stats.elapsed = time.perf_counter() - start
        return found, stats
