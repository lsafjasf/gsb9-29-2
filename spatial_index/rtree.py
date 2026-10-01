"""R-tree 空间索引（Guttman 1984，二次分裂），仅用标准库。

支持：
- 矩形范围查询（闭区间）
- k 最近邻查询（欧氏距离，最佳优先 BFS）
- 插入 / 删除 / 位置变更（move）
- 每次查询返回代价统计：候选数 candidates、访问节点数 nodes_visited、耗时 elapsed
"""

from __future__ import annotations

import heapq
import itertools
import time
from dataclasses import dataclass


@dataclass
class QueryStats:
    candidates: int = 0      # 做过精确几何判定的叶子条目数（点距离 / 点-矩形判定次数）
    nodes_visited: int = 0   # 实际进入的树节点数
    elapsed: float = 0.0     # 查询耗时（秒，perf_counter）


# ---------- MBR 工具 ----------

def _area(mbr):
    """MBR 面积（任意维的超体积）。"""
    result = 1.0
    for lo, hi in zip(mbr[0], mbr[1]):
        result *= hi - lo
    return result


def _union(a, b):
    return (
        tuple(min(x, y) for x, y in zip(a[0], b[0])),
        tuple(max(x, y) for x, y in zip(a[1], b[1])),
    )


def _enlargement(mbr, other):
    return _area(_union(mbr, other)) - _area(mbr)


def _intersects(a, b):
    return all(
        alo <= bhi and blo <= ahi
        for alo, ahi, blo, bhi in zip(a[0], a[1], b[0], b[1])
    )


def _contains_mbr(outer, inner):
    return all(
        olo <= ilo and ihi <= ohi
        for olo, ohi, ilo, ihi in zip(outer[0], outer[1], inner[0], inner[1])
    )


def _mindist(mbr, point):
    """点到 MBR 的最小欧氏距离平方（单调即可，无需开方）。"""
    total = 0.0
    for lo, hi, q in zip(mbr[0], mbr[1], point):
        if q < lo:
            d = lo - q
        elif q > hi:
            d = q - hi
        else:
            d = 0.0
        total += d * d
    return total


class _Node:
    __slots__ = ("leaf", "entries", "parent")

    def __init__(self, leaf):
        self.leaf = leaf
        # 叶子: (mbr, rid)；内部: (mbr, child _Node)
        self.entries = []
        self.parent = None


class RTree:
    def __init__(self, dim, max_entries=8, min_entries=None):
        if dim < 1:
            raise ValueError("dim must be >= 1")
        if max_entries < 4:
            raise ValueError("max_entries must be >= 4")
        self.dim = dim
        self.max_entries = max_entries
        self.min_entries = min_entries if min_entries is not None else max(2, max_entries // 2)
        self.root = _Node(leaf=True)
        self._size = 0

    def __len__(self):
        return self._size

    # ---------- 基本操作 ----------

    def insert(self, rid, point):
        """插入 (rid, point)。rid 需由上层保证唯一。"""
        point = tuple(point)
        if len(point) != self.dim:
            raise ValueError("point dimension mismatch")
        mbr = (point, point)
        leaf = self._choose_leaf(mbr)
        leaf.entries.append((mbr, rid))
        self._size += 1
        if len(leaf.entries) > self.max_entries:
            node, sibling = self._split(leaf)
            self._adjust(node, sibling)
        else:
            self._adjust(leaf, None)

    def delete(self, rid, point):
        """按 (rid, point) 删除，找不到抛 KeyError。"""
        point = tuple(point)
        leaf = self._find_leaf(self.root, (point, point), rid)
        if leaf is None:
            raise KeyError(rid)
        leaf.entries = [(m, r) for m, r in leaf.entries if r != rid]
        self._size -= 1
        self._condense(leaf)

    def move(self, rid, old_point, new_point):
        """位置变更 = 删除旧位置 + 插入新位置（同一事务内完成）。"""
        self.delete(rid, old_point)
        self.insert(rid, new_point)

    def items(self):
        """枚举全部 (rid, point)，供一致性断言使用。"""
        stack = [self.root]
        while stack:
            node = stack.pop()
            if node.leaf:
                for mbr, rid in node.entries:
                    yield rid, mbr[0]
            else:
                for _, child in node.entries:
                    stack.append(child)

    # ---------- 查询 ----------

    def range_query(self, lo, hi):
        """矩形范围查询，闭区间。返回 (rids, QueryStats)。"""
        qmbr = (tuple(lo), tuple(hi))
        if len(qmbr[0]) != self.dim:
            raise ValueError("query dimension mismatch")
        stats = QueryStats()
        start = time.perf_counter()
        found = []
        stack = [self.root]
        while stack:
            node = stack.pop()
            stats.nodes_visited += 1
            if node.leaf:
                for mbr, rid in node.entries:
                    stats.candidates += 1
                    if _intersects(mbr, qmbr):
                        found.append(rid)
            else:
                for mbr, child in node.entries:
                    if _intersects(mbr, qmbr):
                        stack.append(child)
        stats.elapsed = time.perf_counter() - start
        return found, stats

    def knn(self, point, k):
        """k 最近邻，欧氏距离平方排序。返回 (rids, QueryStats)，rids 按距离升序。"""
        point = tuple(point)
        if len(point) != self.dim:
            raise ValueError("query dimension mismatch")
        stats = QueryStats()
        start = time.perf_counter()
        result = []
        if k <= 0 or self._size == 0:
            stats.elapsed = time.perf_counter() - start
            return result, stats
        tie = itertools.count()
        heap = [(_mindist(self._node_mbr(self.root), point), next(tie), 0, self.root)]
        while heap and len(result) < k:
            _, _, kind, obj = heapq.heappop(heap)
            if kind == 1:  # 叶子条目 = 候选点
                stats.candidates += 1
                result.append(obj)
            else:          # 树节点
                stats.nodes_visited += 1
                if obj.leaf:
                    for mbr, rid in obj.entries:
                        heapq.heappush(
                            heap,
                            (_mindist(mbr, point), next(tie), 1, rid),
                        )
                else:
                    for mbr, child in obj.entries:
                        heapq.heappush(
                            heap,
                            (_mindist(mbr, point), next(tie), 0, child),
                        )
        stats.elapsed = time.perf_counter() - start
        return result, stats

    # ---------- 内部算法 ----------

    def _choose_leaf(self, mbr):
        node = self.root
        while not node.leaf:
            best = None
            best_key = None
            for entry in node.entries:
                key = (_enlargement(entry[0], mbr), _area(entry[0]))
                if best_key is None or key < best_key:
                    best_key, best = key, entry
            node = best[1]
        return node

    def _split(self, node):
        """二次分裂（Quadratic Split）。"""
        entries = node.entries
        # 1) 选种子：浪费面积最大的一对
        pair = None
        worst = None
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                waste = (
                    _area(_union(entries[i][0], entries[j][0]))
                    - _area(entries[i][0])
                    - _area(entries[j][0])
                )
                if worst is None or waste > worst:
                    worst, pair = waste, (i, j)
        i, j = pair
        group_a = [entries[i]]
        group_b = [entries[j]]
        rest = entries[:i] + entries[i + 1:j] + entries[j + 1:]
        mbr_a = group_a[0][0]
        mbr_b = group_b[0][0]

        # 2) 依次分配
        while rest:
            if len(group_a) + len(rest) == self.min_entries:
                group_a.extend(rest)
                rest = []
                break
            if len(group_b) + len(rest) == self.min_entries:
                group_b.extend(rest)
                rest = []
                break
            best_k = 0
            best_diff = None
            da_best = db_best = 0.0
            for k, (m, _payload) in enumerate(rest):
                da = _enlargement(mbr_a, m)
                db = _enlargement(mbr_b, m)
                diff = abs(da - db)
                if best_diff is None or diff > best_diff:
                    best_diff, best_k, da_best, db_best = diff, k, da, db
            m, payload = rest.pop(best_k)
            go_a = da_best < db_best
            if da_best == db_best:
                aa, ab = _area(mbr_a), _area(mbr_b)
                if aa == ab:
                    go_a = len(group_a) <= len(group_b)
                else:
                    go_a = aa < ab
            if go_a:
                group_a.append((m, payload))
                mbr_a = _union(mbr_a, m)
            else:
                group_b.append((m, payload))
                mbr_b = _union(mbr_b, m)

        node.entries = group_a
        sibling = _Node(node.leaf)
        sibling.entries = group_b
        sibling.parent = node.parent
        if not node.leaf:
            for _m, child in group_b:
                child.parent = sibling
        return node, sibling

    def _adjust(self, node, sibling):
        """向上调整 MBR，必要时把分裂传播到根。"""
        while True:
            parent = node.parent
            if parent is None:
                if sibling is not None:
                    new_root = _Node(leaf=False)
                    new_root.entries = [
                        (self._node_mbr(node), node),
                        (self._node_mbr(sibling), sibling),
                    ]
                    node.parent = new_root
                    sibling.parent = new_root
                    self.root = new_root
                return
            self._refresh_entry(parent, node)
            if sibling is not None:
                parent.entries.append((self._node_mbr(sibling), sibling))
                sibling.parent = parent
            if len(parent.entries) > self.max_entries:
                node, sibling = self._split(parent)
            else:
                node, sibling = parent, None

    def _find_leaf(self, node, mbr, rid):
        if node.leaf:
            for _m, r in node.entries:
                if r == rid:
                    return node
            return None
        for entry_mbr, child in node.entries:
            if _contains_mbr(entry_mbr, mbr):
                found = self._find_leaf(child, mbr, rid)
                if found is not None:
                    return found
        return None

    def _collect_leaves(self, node, out):
        stack = [node]
        while stack:
            cur = stack.pop()
            if cur.leaf:
                out.extend(cur.entries)
            else:
                for _m, child in cur.entries:
                    stack.append(child)

    def _condense(self, leaf):
        """删除后的收缩：欠载节点摘除，其子树叶子条目重新插入。"""
        orphans = []
        node = leaf
        while node.parent is not None:
            parent = node.parent
            if len(node.entries) < self.min_entries:
                parent.entries = [e for e in parent.entries if e[1] is not node]
                if node.leaf:
                    orphans.extend(node.entries)
                else:
                    self._collect_leaves(node, orphans)
            else:
                self._refresh_entry(parent, node)
            node = parent
        # node 是根
        if not node.leaf:
            if len(node.entries) == 1:
                child = node.entries[0][1]
                child.parent = None
                self.root = child
            elif len(node.entries) == 0:
                self.root = _Node(leaf=True)
        # 重新插入孤儿（条目本来就存在，重插后冲销 _size 计数）
        for mbr, rid in orphans:
            self.insert(rid, mbr[0])
        self._size -= len(orphans)

    def _node_mbr(self, node):
        mbr = node.entries[0][0]
        lo, hi = list(mbr[0]), list(mbr[1])
        for m, _payload in node.entries[1:]:
            for d in range(self.dim):
                if m[0][d] < lo[d]:
                    lo[d] = m[0][d]
                if m[1][d] > hi[d]:
                    hi[d] = m[1][d]
        return (tuple(lo), tuple(hi))

    def _refresh_entry(self, parent, child):
        for idx, (m, c) in enumerate(parent.entries):
            if c is child:
                parent.entries[idx] = (self._node_mbr(child), child)
                return

    # ---------- 结构一致性断言 ----------

    def validate(self):
        """校验父指针、MBR、容量约束以及点条目数。失败抛 AssertionError。"""
        count = self._validate_node(self.root, is_root=True)
        assert count == self._size, f"R-tree size mismatch: {count} vs {self._size}"

    def _validate_node(self, node, is_root):
        if not is_root:
            assert self.min_entries <= len(node.entries) <= self.max_entries, (
                f"node capacity violated: {len(node.entries)}"
            )
        assert node.parent is None if is_root else node.parent is not None
        if node.leaf:
            for mbr, _rid in node.entries:
                assert mbr[0] == mbr[1], "leaf entry must be a point"
            return len(node.entries)
        total = 0
        for mbr, child in node.entries:
            assert child.parent is node, "parent pointer mismatch"
            assert self._node_mbr(child) == mbr, "stale MBR on internal entry"
            total += self._validate_node(child, is_root=False)
        return total
