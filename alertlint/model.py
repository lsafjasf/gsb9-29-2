"""Core data model for alert rules and metric metadata.

A rule is a set of atomic conditions combined with ``all`` (AND) or
``any`` (OR).  Every condition refers to one metric, one aggregation,
one observation window and one threshold comparison.

Only standard library types are used.
"""

from __future__ import annotations

from dataclasses import dataclass, field

GT, GE, LT, LE, EQ, NE = "gt", "ge", "lt", "le", "eq", "ne"
OPS = frozenset({GT, GE, LT, LE, EQ, NE})

AVG, SUM, MIN, MAX, LAST = "avg", "sum", "min", "max", "last"
P50, P90, P95, P99 = "p50", "p90", "p95", "p99"
# Aggregations whose output range equals the raw metric value range.
RANGE_PRESERVING_AGGS = frozenset(
    {AVG, MIN, MAX, LAST, P50, P90, P95, P99}
)
ALL_AGGS = frozenset({SUM} | RANGE_PRESERVING_AGGS)

ALL_ = "all"  # logical AND
ANY_ = "any"  # logical OR
COMBINATORS = frozenset({ALL_, ANY_})


@dataclass(frozen=True)
class MetricMeta:
    """Feasible value range of a metric: every observed sample lies in
    [min, max].  ``sample_interval_seconds`` is the reporting period of
    the source series; it is required to scale ``sum`` aggregations over
    a window.  When absent, ``sum`` ranges are treated as unbounded
    (conservative: no conclusion is drawn)."""

    name: str
    min: float
    max: float
    unit: str = ""
    sample_interval_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.min > self.max:
            raise ValueError(f"metric {self.name!r}: min {self.min} > max {self.max}")
        if not (self.min == self.min and self.max == self.max):
            raise ValueError(f"metric {self.name!r}: min/max must be finite numbers")
        if self.sample_interval_seconds is not None and self.sample_interval_seconds <= 0:
            raise ValueError(f"metric {self.name!r}: sample interval must be positive")


@dataclass(frozen=True)
class Condition:
    metric: str
    op: str
    threshold: float
    agg: str = AVG
    window_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.op not in OPS:
            raise ValueError(f"unsupported op {self.op!r}")
        if self.agg not in ALL_AGGS:
            raise ValueError(f"unsupported agg {self.agg!r}")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if self.threshold != self.threshold:
            raise ValueError("threshold must be a finite number")

    @property
    def key(self) -> tuple:
        """Identity of the aggregated series a condition reads."""
        return self.metric, self.agg, self.window_seconds

    def render(self) -> str:
        return f"{self.agg}({self.metric}, {int(self.window_seconds)}s) {self.op} {self.threshold:g}"


@dataclass
class Rule:
    id: str
    name: str
    conditions: list
    combinator: str = ALL_
    severity: str = "warning"
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.combinator not in COMBINATORS:
            raise ValueError(f"unsupported combinator {self.combinator!r}")
        normalized = []
        for cond in self.conditions:
            if not isinstance(cond, Condition):
                raise TypeError("rule conditions must be Condition instances")
            normalized.append(cond)
        self.conditions = normalized
