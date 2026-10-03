"""取值域推导：用区间集合表示指标（或聚合结果）的可能取值。

所有"可能触发 / 恒触发"结论都建立在同一套区间运算上：
- predicate_region：比较条件在取值域内为真的子区间
- complement_within：为假的子区间
- intersect：组合条件（AND）的可行域求交
- jaccard：两个条件的触发域重合度（重复检测用）

整数域（integer=True）按步长 1 计数，连续域按长度计量；
无界域的度量记为 math.inf，相似度计算时退化为方向+阈值接近度。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .model import Leaf, Metric

# 区间 = (下界, 下界是否闭, 上界, 上界是否闭)；None 表示无界
Bound = Tuple[Optional[float], bool, Optional[float], bool]


@dataclass(frozen=True)
class Domain:
    """一组互不相交、已排序的区间。"""

    intervals: Tuple[Bound, ...]
    integer: bool = False

    # ---------- 基本性质 ----------

    def is_empty(self) -> bool:
        return not self.intervals

    def is_finite(self) -> bool:
        return all(lo is not None and hi is not None for lo, _, hi, _ in self.intervals)

    def measure(self) -> float:
        """连续域长度 / 整数域端点跨度；空域为 0，无界为 inf。"""
        total = 0.0
        for lo, lo_c, hi, hi_c in self.intervals:
            if lo is None or hi is None:
                return math.inf
            length = hi - lo
            if length == 0 and not (lo_c and hi_c):
                continue  # 空开区间，防御性处理
            total += length
        return total

    def count(self) -> float:
        """整数域：可行取值个数；连续域：退化为 measure。"""
        if not self.integer:
            return self.measure()
        total = 0.0
        for lo, lo_c, hi, hi_c in self.intervals:
            if lo is None or hi is None:
                return math.inf
            lo_i = math.ceil(lo if lo_c else lo + 1)
            hi_i = math.floor(hi if hi_c else hi - 1)
            if hi_i >= lo_i:
                total += hi_i - lo_i + 1
        return total

    # ---------- 运算 ----------

    def intersect(self, other: "Domain") -> "Domain":
        out: List[Bound] = []
        for a in self.intervals:
            for b in other.intervals:
                merged = _intersect_bound(a, b)
                if merged is not None:
                    out.append(merged)
        return Domain(tuple(out), self.integer)

    def complement_within(self, universe: "Domain") -> "Domain":
        """universe \\ self：取值域内使谓词为假的部分。"""
        remaining = list(universe.intervals)
        for piece in self.intervals:
            nxt: List[Bound] = []
            for uni in remaining:
                nxt.extend(_subtract(uni, piece))
            remaining = nxt
        return Domain(tuple(remaining), universe.integer)

    def jaccard(self, other: "Domain") -> Optional[float]:
        """|A∩B| / |A∪B|；两侧均无界（度量不可比）时返回 None。"""
        m1 = self.measure()
        m2 = other.measure()
        if math.isinf(m1) or math.isinf(m2):
            return None
        inter = self.intersect(other).measure()
        union = m1 + m2 - inter
        if union == 0:
            return 1.0
        return inter / union


def _intersect_bound(a: Bound, b: Bound) -> Optional[Bound]:
    lo, lo_c = _max_lower(a[0], a[1], b[0], b[1])
    hi, hi_c = _min_upper(a[2], a[3], b[2], b[3])
    if lo is not None and hi is not None:
        if lo > hi or (lo == hi and not (lo_c and hi_c)):
            return None
    return (lo, lo_c, hi, hi_c)


def _max_lower(a_lo, a_c, b_lo, b_c):
    if a_lo is None:
        return b_lo, b_c
    if b_lo is None:
        return a_lo, a_c
    if a_lo > b_lo:
        return a_lo, a_c
    if b_lo > a_lo:
        return b_lo, b_c
    return a_lo, a_c and b_c


def _min_upper(a_hi, a_c, b_hi, b_c):
    if a_hi is None:
        return b_hi, b_c
    if b_hi is None:
        return a_hi, a_c
    if a_hi < b_hi:
        return a_hi, a_c
    if b_hi < a_hi:
        return b_hi, b_c
    return a_hi, a_c and b_c


def _subtract(uni: Bound, piece: Bound) -> List[Bound]:
    """uni \\ piece，piece 与 uni 均为单区间。"""
    u_lo, u_lo_c, u_hi, u_hi_c = uni
    p_lo, p_lo_c, p_hi, p_hi_c = piece
    out: List[Bound] = []
    # 左半部分 [u_lo, p_lo)
    if p_lo is not None and (u_lo is None or u_lo < p_lo or (u_lo == p_lo and (u_lo_c or not p_lo_c))):
        left = (u_lo, u_lo_c, p_lo, not p_lo_c)
        if _non_empty(left):
            out.append(left)
    # 右半部分 (p_hi, u_hi]
    if p_hi is not None and (u_hi is None or p_hi < u_hi or (p_hi == u_hi and (u_hi_c or not p_hi_c))):
        right = (p_hi, not p_hi_c, u_hi, u_hi_c)
        if _non_empty(right):
            out.append(right)
    return out


def _non_empty(bound: Bound) -> bool:
    lo, lo_c, hi, hi_c = bound
    if lo is None or hi is None:
        return True
    if lo < hi:
        return True
    return lo == hi and lo_c and hi_c


def full_domain(metric: Metric) -> Domain:
    return Domain(((metric.min, True, metric.max, True),), metric.integer)


def predicate_region(op: str, value: float, universe: Domain) -> Domain:
    """{x ∈ universe : x op value}。"""
    if op == "<":
        region = Domain(((None, False, value, False),), universe.integer)
    elif op == "<=":
        region = Domain(((None, False, value, True),), universe.integer)
    elif op == ">":
        region = Domain(((value, False, None, False),), universe.integer)
    elif op == ">=":
        region = Domain(((value, True, None, False),), universe.integer)
    elif op == "==":
        region = Domain(((value, True, value, True),), universe.integer)
    elif op == "!=":
        region = Domain(
            ((None, False, value, False), (value, False, None, False)), universe.integer
        )
    else:
        raise ValueError(f"不支持的比较符: {op!r}")
    return region.intersect(universe)


# ---------- 聚合后的取值域 ----------

# 不改变取值范围的聚合方式
_RANGE_PRESERVING_AGGS = {"avg", "mean", "min", "max", "last", "first", "median", "p50", "p90", "p95", "p99"}
# 结果为“样本个数”的聚合：整数域 [0, 窗口内样本数]（样本数未知则无上界）
_COUNT_AGGS = {"count", "count_non_null"}
# 结果为每秒速率的聚合：非负、上界未知
_RATE_AGGS = {"rate", "irate", "increase_per_sec"}


def effective_domain(
    metric: Optional[Metric],
    leaf: Leaf,
) -> Tuple[Optional[Domain], List[str]]:
    """结合聚合方式与窗口，推导 leaf 左值（聚合结果）的取值域。

    返回 (domain, notes)；domain 为 None 表示信息不足、无法推导，
    此时调用方必须保持"未知"结论，不得猜测（误报控制的关键）。
    """
    notes: List[str] = []
    if metric is None:
        return None, notes

    base = full_domain(metric)
    agg = leaf.agg

    if agg in _RANGE_PRESERVING_AGGS:
        return base, notes

    if agg in _COUNT_AGGS:
        hi: Optional[float] = None
        if metric.every_seconds and leaf.window_seconds:
            hi = leaf.window_seconds / metric.every_seconds
        else:
            notes.append("count 聚合缺少采样间隔或窗口，样本数上界未知")
        return Domain(((0.0, True, hi, True),), True), notes

    if agg in _RATE_AGGS:
        return Domain(((0.0, True, None, False),), False), notes

    if agg == "sum":
        if metric.min is None or metric.max is None:
            notes.append("sum 聚合但指标取值域无界，无法推导总量范围")
            return None, notes
        if metric.every_seconds and leaf.window_seconds:
            n = leaf.window_seconds / metric.every_seconds
            return Domain(((metric.min * n, True, metric.max * n, True),), metric.integer), notes
        notes.append("sum 聚合缺少采样间隔或窗口，无法推导总量范围")
        return None, notes

    if agg == "delta":
        if metric.min is None or metric.max is None:
            notes.append("delta 聚合但指标取值域无界，无法推导变化量范围")
            return None, notes
        span = metric.max - metric.min
        return Domain(((-span, True, span, True),), metric.integer), notes

    notes.append(f"未知聚合方式 {agg!r}，取值域按原指标处理")
    return base, notes
