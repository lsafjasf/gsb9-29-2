"""Combined key + spatial index (pure standard library).

- KeyIndex: sorted-array index supporting equality and range queries.
- RTree: Guttman R-tree (quadratic split, condense-on-delete) supporting
  axis-aligned rectangle range queries and best-first k-nearest-neighbour
  queries in arbitrary dimensions.
- SpatialDB: keeps a record store, the key index and the R-tree in sync
  across insert / delete / move, and can assert index-data consistency.

Every query returns (result, QueryStats) where stats carry the cost
metrics: candidates examined, index nodes visited and wall-clock time.
"""

import bisect
import heapq
import time
from dataclasses import dataclass


@dataclass
class QueryStats:
    candidates: int = 0
    nodes_visited: int = 0
    elapsed_ns: int = 0

    @property
    def elapsed_ms(self):
        return self.elapsed_ns / 1e6


# ---------------------------------------------------------------- Rect helpers

def _rect_of_point(p):
    return (tuple(p), tuple(p))


def _rect_union(a, b):
    return (tuple(min(x, y) for x, y in zip(a[0], b[0])),
            tuple(max(x, y) for x, y in zip(a[1], b[1])))


def _rect_area(a):
    v = 1.0
    for lo, hi in zip(a[0], a[1]):
        v *= hi - lo
    return v


def _rect_intersects(a, b):
    return all(al <= bh and bl <= ah for al, ah, bl, bh
               in zip(a[0], a[1], b[0], b[1]))


def _rect_contains_point(a, p):
    return all(lo <= x <= hi for lo, hi, x in zip(a[0], a[1], p))


def _rect_enlargement(a, p):
    return _rect_area(_rect_union(a, _rect_of_point(p))) - _rect_area(a)


def _rect_mindist2(a, p):
    d = 0.0
    for lo, hi, x in zip(a[0], a[1], p):
        if x < lo:
            d += (lo - x) ** 2
        elif x > hi:
            d += (x - hi) ** 2
    return d


def _dist2(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b))


# ---------------------------------------------------------------- Key index

class KeyIndex:
    """Sorted-array key index: equality and inclusive range queries."""

    def __init__(self):
        self._keys = []

    def __len__(self):
        return len(self._keys)

    def insert(self, key):
        i = bisect.bisect_left(self._keys, key)
        if i < len(self._keys) and self._keys[i] == key:
            raise KeyError("duplicate key: %r" % (key,))
        self._keys.insert(i, key)

    def delete(self, key):
        i = bisect.bisect_left(self._keys, key)
        if i >= len(self._keys) or self._keys[i] != key:
            raise KeyError("missing key: %r" % (key,))
        self._keys.pop(i)

    def contains(self, key):
        i = bisect.bisect_left(self._keys, key)
        return i < len(self._keys) and self._keys[i] == key

    def eq_query(self, key):
        t0 = time.perf_counter_ns()
        found = self.contains(key)
        stats = QueryStats(candidates=1 if found else 0,
                           nodes_visited=len(self._keys).bit_length(),
                           elapsed_ns=time.perf_counter_ns() - t0)
        return ([key] if found else []), stats

    def range_query(self, lo, hi):
        t0 = time.perf_counter_ns()
        l = bisect.bisect_left(self._keys, lo)
        r = bisect.bisect_right(self._keys, hi)
        result = list(self._keys[l:r])
        stats = QueryStats(candidates=len(result),
                           nodes_visited=len(self._keys).bit_length(),
                           elapsed_ns=time.perf_counter_ns() - t0)
        return result, stats


# ---------------------------------------------------------------- R-tree

class _Node:
    __slots__ = ("leaf", "entries")

    def __init__(self, leaf):
        self.leaf = leaf
        # leaf:     entries = [(key, point_tuple)]
        # internal: entries = [(child_node, child_rect)]
        self.entries = []


class RTree:
    def __init__(self, dim, max_entries=8):
        if dim < 1:
            raise ValueError("dim must be >= 1")
        if max_entries < 4:
            raise ValueError("max_entries must be >= 4")
        self.dim = dim
        self.max_entries = max_entries
        self.min_entries = max(2, max_entries // 2)
        self.root = _Node(leaf=True)
        self.size = 0

    # -- structural helpers ------------------------------------------------

    def _entry_rect(self, node, entry):
        return _rect_of_point(entry[1]) if node.leaf else entry[1]

    def _node_rect(self, node):
        rect = self._entry_rect(node, node.entries[0])
        for e in node.entries[1:]:
            rect = _rect_union(rect, self._entry_rect(node, e))
        return rect

    def _split(self, node):
        entries = node.entries
        rects = [self._entry_rect(node, e) for e in entries]
        worst, s1, s2 = -1.0, 0, 1
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                waste = (_rect_area(_rect_union(rects[i], rects[j]))
                         - _rect_area(rects[i]) - _rect_area(rects[j]))
                if waste > worst:
                    worst, s1, s2 = waste, i, j
        g1, g2 = [s1], [s2]
        r1, r2 = rects[s1], rects[s2]
        rest = [i for i in range(len(entries)) if i != s1 and i != s2]
        while rest:
            if len(g1) + len(rest) == self.min_entries:
                g1.extend(rest)
                break
            if len(g2) + len(rest) == self.min_entries:
                g2.extend(rest)
                break
            best_i, best_diff, best_grp = None, -1.0, 0
            for i in rest:
                d1 = _rect_area(_rect_union(r1, rects[i])) - _rect_area(r1)
                d2 = _rect_area(_rect_union(r2, rects[i])) - _rect_area(r2)
                diff = abs(d1 - d2)
                if diff > best_diff:
                    best_diff = diff
                    best_i = i
                    best_grp = 0 if (d1 < d2 or (d1 == d2 and len(g1) <= len(g2))) else 1
            rest.remove(best_i)
            if best_grp == 0:
                g1.append(best_i)
                r1 = _rect_union(r1, rects[best_i])
            else:
                g2.append(best_i)
                r2 = _rect_union(r2, rects[best_i])
        node.entries = [entries[i] for i in g1]
        new_node = _Node(leaf=node.leaf)
        new_node.entries = [entries[i] for i in g2]
        return new_node, self._node_rect(new_node)

    # -- insert --------------------------------------------------------------

    def insert(self, key, point):
        point = tuple(point)
        if len(point) != self.dim:
            raise ValueError("point dimension mismatch")
        split = self._insert(self.root, key, point)
        if split is not None:
            new_root = _Node(leaf=False)
            new_root.entries = [(self.root, self._node_rect(self.root)),
                                (split[0], split[1])]
            self.root = new_root
        self.size += 1

    def _insert(self, node, key, point):
        if node.leaf:
            node.entries.append((key, point))
        else:
            idx = min(
                range(len(node.entries)),
                key=lambda i: (_rect_enlargement(node.entries[i][1], point),
                               _rect_area(node.entries[i][1])))
            child, _ = node.entries[idx]
            split = self._insert(child, key, point)
            node.entries[idx] = (child, self._node_rect(child))
            if split is not None:
                node.entries.append(split)
        if len(node.entries) > self.max_entries:
            return self._split(node)
        return None

    # -- delete --------------------------------------------------------------

    def delete(self, key, point):
        point = tuple(point)
        path = []
        if not self._find_leaf(self.root, key, point, path):
            raise KeyError("missing entry: %r" % (key,))
        leaf = path[0]
        for i, (k, p) in enumerate(leaf.entries):
            if k == key:
                del leaf.entries[i]
                break
        self._condense(path)
        self.size -= 1

    def _find_leaf(self, node, key, point, path):
        if node.leaf:
            if any(k == key for k, _ in node.entries):
                path.append(node)
                return True
            return False
        for child, rect in node.entries:
            if _rect_contains_point(rect, point):
                if self._find_leaf(child, key, point, path):
                    path.append(node)
                    return True
        return False

    def _condense(self, path):
        # path is leaf..root
        reinsert = []
        for i in range(len(path) - 1):
            node, parent = path[i], path[i + 1]
            if node is self.root:
                break
            if len(node.entries) < self.min_entries:
                for j, (c, _) in enumerate(parent.entries):
                    if c is node:
                        del parent.entries[j]
                        break
                self._collect_data_entries(node, reinsert)
            else:
                for j, (c, _) in enumerate(parent.entries):
                    if c is node:
                        parent.entries[j] = (c, self._node_rect(node))
                        break
        while not self.root.leaf and len(self.root.entries) == 1:
            self.root = self.root.entries[0][0]
        for key, point in reinsert:
            self.insert(key, point)
            self.size -= 1  # reinsertion must not change logical size

    def _collect_data_entries(self, node, out):
        if node.leaf:
            out.extend(node.entries)
        else:
            for child, _ in node.entries:
                self._collect_data_entries(child, out)

    # -- queries ---------------------------------------------------------------

    def range_query(self, mins, maxs):
        rect = (tuple(mins), tuple(maxs))
        t0 = time.perf_counter_ns()
        stats = QueryStats()
        result = []
        stack = [self.root]
        while stack:
            node = stack.pop()
            stats.nodes_visited += 1
            if node.leaf:
                for key, p in node.entries:
                    stats.candidates += 1
                    if _rect_contains_point(rect, p):
                        result.append(key)
            else:
                for child, child_rect in node.entries:
                    if _rect_intersects(child_rect, rect):
                        stack.append(child)
        stats.elapsed_ns = time.perf_counter_ns() - t0
        return result, stats

    def knn(self, point, k):
        point = tuple(point)
        t0 = time.perf_counter_ns()
        stats = QueryStats()
        result = []
        if k > 0:
            seq = 0
            heap = [(0.0, seq, False, self.root)]
            while heap and len(result) < k:
                _, _, is_point, obj = heapq.heappop(heap)
                if is_point:
                    stats.candidates += 1
                    result.append(obj[0])
                else:
                    stats.nodes_visited += 1
                    if obj.leaf:
                        for key, p in obj.entries:
                            seq += 1
                            heapq.heappush(heap, (_dist2(p, point), seq, True, (key, p)))
                    else:
                        for child, child_rect in obj.entries:
                            seq += 1
                            heapq.heappush(heap, (_rect_mindist2(child_rect, point),
                                                  seq, False, child))
        stats.elapsed_ns = time.perf_counter_ns() - t0
        return result, stats

    # -- introspection / validation -------------------------------------------

    def iter_entries(self):
        out = []
        self._collect_data_entries(self.root, out)
        return out

    def count_nodes(self):
        total = 0
        stack = [self.root]
        while stack:
            node = stack.pop()
            total += 1
            if not node.leaf:
                stack.extend(c for c, _ in node.entries)
        return total

    def validate_structure(self):
        """Assert MBR integrity and fill-factor invariants; return entry list."""
        entries = self._validate_node(self.root, is_root=True)
        return entries

    def _validate_node(self, node, is_root=False):
        if not is_root:
            assert self.min_entries <= len(node.entries) <= self.max_entries, \
                "fill factor violated: %d entries" % len(node.entries)
        if node.leaf:
            return list(node.entries)
        collected = []
        for child, rect in node.entries:
            assert rect == self._node_rect(child), "stale MBR detected"
            collected.extend(self._validate_node(child))
        return collected


# ---------------------------------------------------------------- Combined DB

class SpatialDB:
    """Record store kept in sync with a key index and an R-tree."""

    def __init__(self, dim, max_entries=8):
        self.dim = dim
        self._records = {}  # key -> (point_tuple, payload)
        self._keys = KeyIndex()
        self._rtree = RTree(dim, max_entries=max_entries)

    def __len__(self):
        return len(self._records)

    # -- mutations -------------------------------------------------------------

    def insert(self, key, point, payload=None):
        if key in self._records:
            raise KeyError("duplicate key: %r" % (key,))
        point = tuple(point)
        if len(point) != self.dim:
            raise ValueError("point dimension mismatch")
        self._records[key] = (point, payload)
        try:
            self._keys.insert(key)
            self._rtree.insert(key, point)
        except Exception:
            self._records.pop(key, None)
            if self._keys.contains(key):
                self._keys.delete(key)
            raise

    def delete(self, key):
        point, _ = self._records[key]
        self._rtree.delete(key, point)
        self._keys.delete(key)
        del self._records[key]

    def move(self, key, new_point):
        old_point, payload = self._records[key]
        new_point = tuple(new_point)
        if len(new_point) != self.dim:
            raise ValueError("point dimension mismatch")
        self._rtree.delete(key, old_point)
        self._rtree.insert(key, new_point)
        self._records[key] = (new_point, payload)

    # -- queries ---------------------------------------------------------------

    def get(self, key):
        return self._records.get(key)

    def key_eq(self, key):
        keys, stats = self._keys.eq_query(key)
        return [(k, self._records[k]) for k in keys], stats

    def key_range(self, lo, hi):
        keys, stats = self._keys.range_query(lo, hi)
        return [(k, self._records[k]) for k in keys], stats

    def rect_query(self, mins, maxs):
        return self._rtree.range_query(mins, maxs)

    def knn(self, point, k):
        return self._rtree.knn(point, k)

    # -- consistency -------------------------------------------------------------

    def assert_consistent(self):
        """Assert that both indexes exactly mirror the record store."""
        entries = self._rtree.validate_structure()
        rtree_map = {}
        for key, point in entries:
            assert key not in rtree_map, "duplicate key in R-tree: %r" % (key,)
            rtree_map[key] = point
        assert len(rtree_map) == self._rtree.size, "R-tree size counter drift"
        assert set(rtree_map) == set(self._records), \
            "R-tree keys differ from record store"
        for key, point in rtree_map.items():
            assert point == self._records[key][0], \
                "stale point for key %r" % (key,)
        assert list(self._keys._keys) == sorted(self._records), \
            "key index differs from record store"
        return True

    def node_count(self):
        return self._rtree.count_nodes()
