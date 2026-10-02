"""Single-rule triggerability: never / always / ok derivations."""

import unittest

from alertlint.analysis import (ALWAYS, NEVER, OK, UNKNOWN, agg_value_range,
                                analyze_condition, analyze_rule)
from alertlint.model import Condition, MetricMeta, Rule

CPU = MetricMeta("cpu", 0, 100, sample_interval_seconds=60)
RPS = MetricMeta("rps", 0, 50000, sample_interval_seconds=60)
NOINT = MetricMeta("noint", 0, 100)  # no sample interval registered
REG = {m.name: m for m in (CPU, RPS, NOINT)}


def cond(metric="cpu", op="gt", threshold=90, agg="avg", window=300):
    return Condition(metric=metric, op=op, threshold=threshold,
                     agg=agg, window_seconds=window)


def rule_of(*conds, combinator="all"):
    return Rule(id="T", name="T", conditions=list(conds), combinator=combinator)


class TestSingleCondition(unittest.TestCase):
    def test_never_above_max(self):
        v = analyze_condition(cond(op="gt", threshold=100), CPU)
        self.assertEqual(v.status, NEVER)

    def test_never_far_above_max(self):
        v = analyze_condition(cond(op="gt", threshold=150), CPU)
        self.assertEqual(v.status, NEVER)
        self.assertIn("disjoint", v.reason)

    def test_ok_at_max_inclusive(self):
        v = analyze_condition(cond(op="ge", threshold=100), CPU)
        self.assertEqual(v.status, OK)  # fires exactly at 100

    def test_always_ge_min(self):
        v = analyze_condition(cond(op="ge", threshold=0), CPU)
        self.assertEqual(v.status, ALWAYS)

    def test_ok_gt_min(self):
        v = analyze_condition(cond(op="gt", threshold=0), CPU)
        self.assertEqual(v.status, OK)  # silent only exactly at 0

    def test_always_le_max(self):
        v = analyze_condition(cond(op="le", threshold=100), CPU)
        self.assertEqual(v.status, ALWAYS)

    def test_never_lt_min(self):
        v = analyze_condition(cond(op="lt", threshold=0), CPU)
        self.assertEqual(v.status, NEVER)

    def test_ok_le_min(self):
        v = analyze_condition(cond(op="le", threshold=0), CPU)
        self.assertEqual(v.status, OK)

    def test_eq_inside_range_ok(self):
        v = analyze_condition(cond(op="eq", threshold=50), CPU)
        self.assertEqual(v.status, OK)

    def test_eq_outside_range_never(self):
        v = analyze_condition(cond(op="eq", threshold=101), CPU)
        self.assertEqual(v.status, NEVER)

    def test_ne_outside_range_always(self):
        v = analyze_condition(cond(op="ne", threshold=200), CPU)
        self.assertEqual(v.status, ALWAYS)

    def test_ne_inside_range_ok(self):
        v = analyze_condition(cond(op="ne", threshold=50), CPU)
        self.assertEqual(v.status, OK)

    def test_normal_threshold_ok(self):
        v = analyze_condition(cond(op="gt", threshold=90), CPU)
        self.assertEqual(v.status, OK)
        self.assertEqual(str(v.feasible), "(90, 100]")


class TestAggregationRanges(unittest.TestCase):
    def test_avg_range_equals_metric_range_any_window(self):
        for window in (10, 300, 86400, 86400 * 30):
            rng = agg_value_range(CPU, "avg", window)
            self.assertEqual((rng.lo, rng.hi), (0, 100))

    def test_percentile_range_preserved(self):
        rng = agg_value_range(CPU, "p99", 3600)
        self.assertEqual((rng.lo, rng.hi), (0, 100))

    def test_sum_scales_with_window(self):
        rng = agg_value_range(RPS, "sum", 3600)  # 60 samples
        self.assertEqual((rng.lo, rng.hi), (0, 3_000_000))

    def test_sum_long_window_stays_feasible(self):
        # 24h sum of rps: max 72M; a 50M threshold is legitimate.
        v = analyze_condition(
            cond(metric="rps", op="gt", threshold=50_000_000,
                 agg="sum", window=86400), RPS)
        self.assertEqual(v.status, OK)

    def test_sum_above_scaled_max_never(self):
        v = analyze_condition(
            cond(metric="rps", op="gt", threshold=80_000_000,
                 agg="sum", window=86400), RPS)
        self.assertEqual(v.status, NEVER)

    def test_sum_without_sample_interval_is_conservative(self):
        # No sample interval -> unbounded range -> no conclusion, no FP.
        v = analyze_condition(
            cond(metric="noint", op="gt", threshold=10**12,
                 agg="sum", window=86400), NOINT)
        self.assertEqual(v.status, OK)

    def test_unknown_metric_is_unknown_not_never(self):
        v = analyze_condition(cond(metric="ghost", op="gt", threshold=1), None)
        self.assertEqual(v.status, UNKNOWN)


class TestRuleLevel(unittest.TestCase):
    def test_single_rule_verdicts(self):
        self.assertEqual(analyze_rule(rule_of(cond(op="gt", threshold=150)), REG).status, NEVER)
        self.assertEqual(analyze_rule(rule_of(cond(op="ge", threshold=0)), REG).status, ALWAYS)
        self.assertEqual(analyze_rule(rule_of(cond(op="gt", threshold=90)), REG).status, OK)


if __name__ == "__main__":
    unittest.main()
