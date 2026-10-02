"""Triggerability analysis: decide whether a rule can ever fire, always
fires, or is fine, derived from threshold, window, aggregation and the
metric's feasible value range.

Soundness policy (controls false positives):
  * ``never`` is concluded only when the trigger region is *provably*
    disjoint from every value the aggregated series can take.
  * ``always`` is concluded only when the trigger region *provably
    covers* the whole feasible range.
  * Anything uncertain (unknown metric, unbounded sum range, ...) is
    reported as ``unknown``/``ok`` and never as a defect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math

from .intervals import INF, Interval, condition_trigger_interval
from .model import ALL_, ANY_, SUM, Condition, MetricMeta, Rule

OK = "ok"
NEVER = "never"
ALWAYS = "always"
UNKNOWN = "unknown"


def agg_value_range(metric: MetricMeta, agg: str, window_seconds: float) -> Interval:
    """Feasible range of the aggregated series.

    For range-preserving aggregations (avg/min/max/last/percentiles) the
    output range equals the raw metric range, for *any* window length --
    this is why long windows never cause false positives here.

    For ``sum`` the range scales with the number of samples in the
    window, ``window / sample_interval``.  Without a known sample
    interval the range is unbounded and no conclusion is drawn.
    """
    if agg == SUM:
        if metric.sample_interval_seconds is None:
            return Interval(-INF, INF)
        count = window_seconds / metric.sample_interval_seconds
        lo = min(metric.min * count, metric.max * count)
        hi = max(metric.min * count, metric.max * count)
        return Interval(lo, hi)
    return Interval(metric.min, metric.max)


@dataclass
class ConditionVerdict:
    condition: Condition
    status: str  # OK | NEVER | ALWAYS | UNKNOWN
    agg_range: Interval | None
    feasible: Interval | None  # trigger region clipped to agg_range
    reason: str


def analyze_condition(cond: Condition, metric: MetricMeta | None) -> ConditionVerdict:
    if metric is None:
        return ConditionVerdict(
            cond, UNKNOWN, None, None,
            f"metric {cond.metric!r} is not in the registry; no range to reason with",
        )
    rng = agg_value_range(metric, cond.agg, cond.window_seconds)

    if cond.op == "ne":
        return _analyze_ne(cond, rng)

    trigger = condition_trigger_interval(cond.op, cond.threshold)
    feasible = rng.intersect(trigger)
    if feasible.is_empty():
        return ConditionVerdict(
            cond, NEVER, rng, feasible,
            f"trigger {cond.op} {cond.threshold:g} is disjoint from feasible "
            f"range {rng} of {cond.agg}({cond.metric})",
        )
    if feasible.contains(rng) and rng.contains(feasible):
        return ConditionVerdict(
            cond, ALWAYS, rng, feasible,
            f"trigger {cond.op} {cond.threshold:g} covers the whole feasible "
            f"range {rng} of {cond.agg}({cond.metric})",
        )
    return ConditionVerdict(
        cond, OK, rng, feasible,
        f"fires when {cond.agg}({cond.metric}) is in {feasible}",
    )


def _analyze_ne(cond: Condition, rng: Interval) -> ConditionVerdict:
    t = cond.threshold
    inside = (rng.lo < t < rng.hi) or (
        (t == rng.lo and rng.lo_closed) or (t == rng.hi and rng.hi_closed)
    )
    if not inside:
        return ConditionVerdict(
            cond, ALWAYS, rng, rng,
            f"{t:g} lies outside feasible range {rng}; != {t:g} always holds",
        )
    if rng.lo == rng.hi == t:
        return ConditionVerdict(
            cond, NEVER, rng, Interval.empty(),
            f"feasible range is the single point {{{t:g}}}; != {t:g} never holds",
        )
    return ConditionVerdict(
        cond, OK, rng, rng,
        f"fires except at the single point {t:g} inside {rng}",
    )


@dataclass
class RuleAnalysis:
    rule: Rule
    status: str  # OK | NEVER | ALWAYS | UNKNOWN
    condition_verdicts: list = field(default_factory=list)
    reasons: list = field(default_factory=list)


def analyze_rule(rule: Rule, registry: dict) -> RuleAnalysis:
    verdicts = [
        analyze_condition(cond, registry.get(cond.metric))
        for cond in rule.conditions
    ]
    if not rule.conditions:
        return RuleAnalysis(rule, UNKNOWN, verdicts, ["rule has no conditions"])

    if rule.combinator == ALL_:
        return _analyze_and(rule, verdicts, registry)
    return _analyze_or(rule, verdicts, registry)


def _group_intervals(rule: Rule, registry: dict) -> dict:
    """Per (metric, agg, window) trigger intervals, clipped to range."""
    groups: dict = {}
    for cond in rule.conditions:
        metric = registry.get(cond.metric)
        if metric is None:
            continue
        rng = agg_value_range(metric, cond.agg, cond.window_seconds)
        if cond.op == "ne":
            # complement of a point: approximated by the full range for
            # intersection purposes (exact for emptiness/coverage checks
            # because a point never empties nor covers a real range).
            trigger = rng
        else:
            trigger = rng.intersect(condition_trigger_interval(cond.op, cond.threshold))
        groups.setdefault(cond.key, []).append((trigger, rng))
    return groups


def _analyze_and(rule: Rule, verdicts, registry: dict) -> RuleAnalysis:
    reasons = []
    groups = _group_intervals(rule, registry)
    unknown_metrics = [v for v in verdicts if v.status == UNKNOWN]

    for (metric, agg, window), entries in groups.items():
        rng = entries[0][1]
        feasible = rng
        for trigger, _ in entries:
            feasible = feasible.intersect(trigger)
        if feasible.is_empty():
            if len(entries) == 1 and len(rule.conditions) == 1:
                detail = (f"trigger condition is disjoint from feasible "
                          f"range {rng} of {agg}({metric}, {int(window)}s)")
            else:
                detail = (f"AND-contradiction on {agg}({metric}, {int(window)}s): "
                          f"combined conditions are disjoint from feasible range {rng}")
            reasons.append(detail)
            return RuleAnalysis(rule, NEVER, verdicts, reasons)

    if unknown_metrics:
        names = ", ".join(sorted({v.condition.metric for v in unknown_metrics}))
        reasons.append(f"unknown metric(s) {names}; remaining conditions are satisfiable")
        return RuleAnalysis(rule, UNKNOWN, verdicts, reasons)

    for (metric, agg, window), entries in groups.items():
        rng = entries[0][1]
        feasible = rng
        for trigger, _ in entries:
            feasible = feasible.intersect(trigger)
        if not (feasible.contains(rng) and rng.contains(feasible)):
            return RuleAnalysis(rule, OK, verdicts,
                                [f"fires when all conditions hold; e.g. "
                                 f"{agg}({metric}) in {feasible}"])
    reasons.append("every condition covers its whole feasible range; "
                   "the AND of them always holds")
    return RuleAnalysis(rule, ALWAYS, verdicts, reasons)


def _analyze_or(rule: Rule, verdicts, registry: dict) -> RuleAnalysis:
    reasons = []
    groups = _group_intervals(rule, registry)
    unknown_metrics = [v for v in verdicts if v.status == UNKNOWN]

    # NEVER: every known condition is individually infeasible.
    any_feasible = False
    for entries in groups.values():
        for trigger, _ in entries:
            if not trigger.is_empty():
                any_feasible = True
    if not any_feasible and not unknown_metrics:
        reasons.append("every OR-branch is disjoint from its feasible range")
        return RuleAnalysis(rule, NEVER, verdicts, reasons)

    # ALWAYS: some metric's branches jointly cover its whole range.
    for (metric, agg, window), entries in groups.items():
        rng = entries[0][1]
        if rng.lo == -INF or rng.hi == INF:
            continue  # cannot prove coverage of an unbounded range
        pieces = sorted((t for t, _ in entries if not t.is_empty()), key=lambda iv: iv.lo)
        merged = []
        for iv in pieces:
            if not merged:
                merged.append(iv)
                continue
            cur = merged[-1]
            touch = iv.lo < cur.hi or (
                iv.lo == cur.hi and (iv.lo_closed or cur.hi_closed)
            )
            if touch:
                hi = max(cur.hi, iv.hi)
                hi_closed = cur.hi_closed if cur.hi > iv.hi else (
                    iv.hi_closed if iv.hi > cur.hi else (cur.hi_closed and iv.hi_closed)
                )
                merged[-1] = Interval(cur.lo, hi, cur.lo_closed, hi_closed)
            else:
                merged.append(iv)
        if any(piece.contains(rng) for piece in merged):
            reasons.append(
                f"OR-branches on {agg}({metric}, {int(window)}s) jointly "
                f"cover the whole feasible range {rng}"
            )
            return RuleAnalysis(rule, ALWAYS, verdicts, reasons)

    if unknown_metrics:
        names = ", ".join(sorted({v.condition.metric for v in unknown_metrics}))
        reasons.append(f"unknown metric(s) {names}; remaining conditions are satisfiable")
        return RuleAnalysis(rule, UNKNOWN, verdicts, reasons)

    for (metric, agg, window), entries in groups.items():
        rng = entries[0][1]
        feasible = rng
        for trigger, _ in entries:
            feasible = feasible.intersect(trigger)
        if not (feasible.contains(rng) and rng.contains(feasible)):
            return RuleAnalysis(rule, OK, verdicts,
                                [f"fires when all conditions hold; e.g. "
                                 f"{agg}({metric}) in {feasible}"])
    reasons.append("every condition covers its whole feasible range; "
                   "the AND of them always holds")
    return RuleAnalysis(rule, ALWAYS, verdicts, reasons)


def _analyze_or(rule: Rule, verdicts, registry: dict) -> RuleAnalysis:
    reasons = []
    groups = _group_intervals(rule, registry)
    unknown_metrics = [v for v in verdicts if v.status == UNKNOWN]

    # NEVER: every known condition is individually infeasible.
    any_feasible = False
    for entries in groups.values():
        for trigger, _ in entries:
            if not trigger.is_empty():
                any_feasible = True
    if not any_feasible and not unknown_metrics:
        reasons.append("every OR-branch is disjoint from its feasible range")
        return RuleAnalysis(rule, NEVER, verdicts, reasons)

    # ALWAYS: some metric's branches jointly cover its whole range.
    for (metric, agg, window), entries in groups.items():
        rng = entries[0][1]
        if rng.lo == -INF or rng.hi == INF:
            continue  # cannot prove coverage of an unbounded range
        covered = Interval.empty()
        ok = True
        pieces = sorted((t for t, _ in entries), key=lambda iv: iv.lo)
        cur = None
        union = []
        for iv in pieces:
            if cur is None:
                cur = iv
            elif iv.lo < cur.hi or (iv.lo == cur.hi and (iv.lo_closed or cur.hi_closed)):
                cur = Interval(cur.lo, max(cur.hi, iv.hi), cur.lo_closed,
                               iv.hi_closed if iv.hi >= cur.hi else cur.hi_closed)
            else:
                union.append(cur)
                cur = iv
        if cur is not None:
            union.append(cur)
        for piece in union:
            covered = covered  # keep simple: check single-piece coverage
            if piece.contains(rng):
                reasons.append(
                    f"OR-branches on {agg}({metric}, {int(window)}s) jointly "
                    f"cover the whole feasible range {rng}"
                )
                return RuleAnalysis(rule, ALWAYS, verdicts, reasons)

    if unknown_metrics:
        names = ", ".join(sorted({v.condition.metric for v in unknown_metrics}))
        reasons.append(f"unknown metric(s) {names}")
        return RuleAnalysis(rule, UNKNOWN, verdicts, reasons)
    return RuleAnalysis(rule, OK, verdicts, ["at least one OR-branch can fire"])
