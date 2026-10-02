"""Closed/open real intervals used for threshold feasibility reasoning.

An ``Interval`` is a connected subset of the real line.  Bounds may be
infinite and either included or excluded, which matters at threshold
boundaries: ``x > max`` must be empty while ``x >= max`` is the
single point ``{max}``.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

INF = math.inf


@dataclass(frozen=True)
class Interval:
    lo: float
    hi: float
    lo_closed: bool = True
    hi_closed: bool = True

    @staticmethod
    def empty() -> "Interval":
        return Interval(0.0, -1.0, False, False)

    @staticmethod
    def point(value: float) -> "Interval":
        return Interval(value, value, True, True)

    def is_empty(self) -> bool:
        if self.lo > self.hi:
            return True
        if self.lo == self.hi and not (self.lo_closed and self.hi_closed):
            return True
        return False

    def intersect(self, other: "Interval") -> "Interval":
        if self.is_empty() or other.is_empty():
            return Interval.empty()
        if self.lo > other.lo:
            lo, lo_closed = self.lo, self.lo_closed
        elif self.lo < other.lo:
            lo, lo_closed = other.lo, other.lo_closed
        else:
            lo = self.lo
            lo_closed = self.lo_closed and other.lo_closed
        if self.hi < other.hi:
            hi, hi_closed = self.hi, self.hi_closed
        elif self.hi > other.hi:
            hi, hi_closed = other.hi, other.hi_closed
        else:
            hi = self.hi
            hi_closed = self.hi_closed and other.hi_closed
        result = Interval(lo, hi, lo_closed, hi_closed)
        return Interval.empty() if result.is_empty() else result

    def contains(self, other: "Interval") -> bool:
        """True iff self (as a set) is a superset of other."""
        if other.is_empty():
            return True
        if self.is_empty():
            return False
        if other.lo < self.lo:
            return False
        if other.lo == self.lo and not self.lo_closed and other.lo_closed:
            return False
        if other.hi > self.hi:
            return False
        if other.hi == self.hi and not self.hi_closed and other.hi_closed:
            return False
        return True

    def measure(self) -> float:
        if self.is_empty():
            return 0.0
        return float(self.hi - self.lo)

    def __str__(self) -> str:
        if self.is_empty():
            return "empty"
        lb = "[" if self.lo_closed else "("
        rb = "]" if self.hi_closed else ")"
        return f"{lb}{_fmt(self.lo)}, {_fmt(self.hi)}{rb}"


def _fmt(value: float) -> str:
    if value == INF:
        return "+inf"
    if value == -INF:
        return "-inf"
    return f"{value:g}"


def condition_trigger_interval(op: str, threshold: float) -> Interval:
    """Set of values that satisfy a single comparison (unclipped)."""
    if op == "gt":
        return Interval(threshold, INF, False, False)
    if op == "ge":
        return Interval(threshold, INF, True, False)
    if op == "lt":
        return Interval(-INF, threshold, False, False)
    if op == "le":
        return Interval(-INF, threshold, False, True)
    if op == "eq":
        return Interval.point(threshold)
    raise ValueError(f"{op!r} is not representable as a connected interval")


def union_measure(intervals) -> float:
    """Lebesgue measure of the union of (possibly open/overlapping) intervals.

    Point gaps (e.g. ``(0,1)`` and ``(1,2)``) have measure zero, so
    intervals are merged whenever ``next.lo <= current.hi``.
    """
    pieces = sorted(
        (iv for iv in intervals if not iv.is_empty()),
        key=lambda iv: iv.lo,
    )
    total = 0.0
    cur_lo = cur_hi = None
    for iv in pieces:
        if cur_lo is None:
            cur_lo, cur_hi = iv.lo, iv.hi
        elif iv.lo <= cur_hi:
            cur_hi = max(cur_hi, iv.hi)
        else:
            total += cur_hi - cur_lo
            cur_lo, cur_hi = iv.lo, iv.hi
    if cur_lo is not None:
        total += cur_hi - cur_lo
    return total


def intersection_measure(list_a, list_b) -> float:
    """Measure of the overlap between two lists of intervals."""
    total = 0.0
    for a in list_a:
        if a.is_empty():
            continue
        for b in list_b:
            if b.is_empty():
                continue
            total += a.intersect(b).measure()
    return total


def jaccard(list_a, list_b) -> float:
    """Jaccard overlap of two interval lists by Lebesgue measure.

    Returns 1.0 when both sets are empty (identical trigger regions),
    and degrades to 0.0 when measure is infinite/uncomputable."""
    list_a = [iv for iv in list_a if not iv.is_empty()]
    list_b = [iv for iv in list_b if not iv.is_empty()]
    if not list_a and not list_b:
        return 1.0
    inter = intersection_measure(list_a, list_b)
    union = union_measure(list_a) + union_measure(list_b) - inter
    if union == 0.0:
        if inter == 0.0 and len(list_a) == len(list_b) == 1 and list_a[0].lo == list_b[0].lo:
            return 1.0
        return 0.0
    if math.isinf(union):
        return 1.0 if math.isinf(inter) and union_measure(list_a) == union_measure(list_b) else 0.0
    return inter / union
