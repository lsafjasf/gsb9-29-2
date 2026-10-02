"""Duplicate / overlap detection between rules.

Three kinds of findings, each with a numeric similarity basis:

  * ``equivalent`` -- canonical condition sets are identical (score 1.0),
    or the trigger regions coincide exactly.
  * ``contained``  -- one AND-rule's conditions are a strict subset of
    another's, so the stricter rule firing always implies the looser
    one fires.  Score is the Jaccard index of the condition sets.
  * ``overlap``    -- trigger regions on shared (metric, agg, window)
    series overlap with Jaccard >= the configured threshold.

Tiered thresholds (e.g. warning at >90, critical at >99) are a
legitimate pattern: their region Jaccard is low, so they are *not*
flagged.  This is the main false-positive guard of this module.
"""

from __future__ import annotations

from dataclasses import dataclass

from .analysis import agg_value_range
from .intervals import (Interval, condition_trigger_interval,
                        intersection_measure, jaccard, union_measure)
from .model import ALL_, ANY_, Rule


def canonical_condition(cond) -> tuple:
    return (cond.metric, cond.agg, cond.window_seconds, cond.op, cond.threshold)


def canonical_rule(rule: Rule) -> tuple:
    """Order-insensitive canonical form.  The combinator is irrelevant
    for single-condition rules."""
    conds = tuple(sorted(canonical_condition(c) for c in rule.conditions))
    combinator = rule.combinator if len(conds) > 1 else ALL_
    return combinator, conds


def trigger_region(rule: Rule, registry: dict):
    """Map (metric, agg, window) -> list of trigger Intervals.

    AND-rules: one intersected interval per series.
    OR-rules:  the list of per-branch intervals per series.
    Returns None when a condition references an unknown metric.
    """
    groups: dict = {}
    for cond in rule.conditions:
        metric = registry.get(cond.metric)
        if metric is None:
            return None
        rng = agg_value_range(metric, cond.agg, cond.window_seconds)
        if cond.op == "ne":
            trigger = rng
        else:
            trigger = rng.intersect(condition_trigger_interval(cond.op, cond.threshold))
        groups.setdefault(cond.key, []).append(trigger)
    if rule.combinator == ALL_:
        merged = {}
        for key, ivs in groups.items():
            acc = ivs[0]
            for iv in ivs[1:]:
                acc = acc.intersect(iv)
            merged[key] = [acc]
        return merged
    return groups


@dataclass
class Similarity:
    kind: str  # "equivalent" | "contained" | "overlap" | "none"
    score: float
    containment: float  # max directional containment of trigger regions
    detail: str


def _region_measures(region):
    return {k: union_measure(v) for k, v in region.items()}


def _combine(scores, comb1, comb2):
    if comb1 == comb2 == ALL_:
        return min(scores)
    if comb1 == comb2 == ANY_:
        return max(scores)
    return sum(scores) / len(scores)


def pair_similarity(r1: Rule, r2: Rule, registry: dict) -> Similarity:
    if canonical_rule(r1) == canonical_rule(r2):
        return Similarity("equivalent", 1.0, 1.0,
                          "identical canonical condition sets")

    set1 = {canonical_condition(c) for c in r1.conditions}
    set2 = {canonical_condition(c) for c in r2.conditions}

    if set1 == set2 and len(set1) > 1:
        return Similarity("overlap", 0.9, 1.0,
                          "same conditions but different combinator "
                          f"({r1.combinator} vs {r2.combinator})")

    if r1.combinator == r2.combinator == ALL_ and (set1 < set2 or set2 < set1):
        inter = len(set1 & set2)
        union = len(set1 | set2)
        narrower = r2.id if set1 < set2 else r1.id
        wider = r1.id if set1 < set2 else r2.id
        return Similarity(
            "contained", inter / union, 1.0,
            f"conditions of {wider} are a strict subset of {narrower}; "
            f"whenever {narrower} fires, {wider} fires too "
            f"(condition-set Jaccard {inter}/{union})",
        )

    reg1 = trigger_region(r1, registry)
    reg2 = trigger_region(r2, registry)
    if reg1 is None or reg2 is None:
        return Similarity("none", 0.0, 0.0, "unknown metric; cannot compare")

    keys1, keys2 = set(reg1), set(reg2)
    if keys1 != keys2:
        # Rules read different series.  Partial-key comparison cannot
        # establish equivalence/high-overlap (a broad OR like
        # "errors OR node-down" shares a key with "node-down" but is
        # not a duplicate); exact-condition subset cases are handled
        # above as contained findings.
        return Similarity("none", 0.0, 0.0,
                          "different (metric, agg, window) series sets")

    shared = keys1
    m1, m2 = _region_measures(reg1), _region_measures(reg2)
    if all(not iv for iv in m1.values()) and all(not iv for iv in m2.values()):
        return Similarity("none", 0.0, 0.0,
                          "both trigger regions are empty; reported as never-fire individually")
    key_scores, key_contain = [], []
    parts = []
    for key in sorted(shared):
        ivs1, ivs2 = reg1[key], reg2[key]
        jac = jaccard(ivs1, ivs2)
        inter = intersection_measure(ivs1, ivs2)
        smaller = min(m1[key], m2[key])
        if smaller == 0:
            contain = 1.0 if inter == 0 and m1[key] == m2[key] else 0.0
        else:
            contain = min(1.0, inter / smaller)
        key_scores.append(jac)
        key_contain.append(contain)
        metric, agg, window = key
        parts.append(
            f"{agg}({metric}, {int(window)}s): "
            f"{_fmt_region(ivs1)} vs {_fmt_region(ivs2)} "
            f"Jaccard={jac:.2f}"
        )
    score = _combine(key_scores, r1.combinator, r2.combinator)
    containment = _combine(key_contain, r1.combinator, r2.combinator)
    kind = "equivalent" if score >= 0.999 else "overlap"
    return Similarity(kind, score, containment, "; ".join(parts))


def _fmt_region(ivs) -> str:
    return " U ".join(str(iv) for iv in ivs)


def find_duplicates(rules, registry: dict, threshold: float = 0.8,
                    contained_threshold: float = 0.5):
    """All flagged pairs: list of (rule_a, rule_b, Similarity)."""
    findings = []
    for i in range(len(rules)):
        for j in range(i + 1, len(rules)):
            sim = pair_similarity(rules[i], rules[j], registry)
            if sim.kind == "equivalent":
                findings.append((rules[i], rules[j], sim))
            elif sim.kind == "contained" and sim.score >= contained_threshold:
                findings.append((rules[i], rules[j], sim))
            elif sim.kind == "overlap" and sim.score >= threshold:
                findings.append((rules[i], rules[j], sim))
    return findings
