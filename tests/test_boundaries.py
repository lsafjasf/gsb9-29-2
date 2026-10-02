"""Threshold boundary cases: threshold exactly on the feasible-range
edge, strict vs non-strict operators, degenerate ranges, sum edges."""

import unittest

from alertlint.analysis import ALWAYS, NEVER, OK, agg_value_range, analyze_condition
from alertlint.intervals import Interval
from alertlint.model import Condition, MetricMeta

CPU = MetricMeta("cpu", 0, 100, sample_interval_seconds=60)
CONST = MetricMeta("const", 7, 7, sample_interval_seconds=60)  # constant metric
RPS = MetricMeta("rps", 0, 50000, sample_interval_seconds=60)


def analyze(op, threshold, metric=CPU, agg="avg", window=300):
    return analyze_condition(
        Condition(metric=metric.name, op=op, threshold=threshold,
                  agg=agg, window_seconds=window), metric)


class TestRangeEdges(unittest.TestCase):
    def test_gt_max_never(self):
        self.assertEqual(analyze("gt", 100).status, NEVER)

    def test_ge_max_ok(self):
        self.assertEqual(analyze("ge", 100).status, OK)

    def test_lt_min_never(self):
        self.assertEqual(analyze("lt", 0).status, NEVER)

    def test_le_min_ok(self):
        self.assertEqual(analyze("le", 0).status, OK)

    def test_ge_min_always(self):
        self.assertEqual(analyze("ge", 0).status, ALWAYS)

    def test_gt_min_ok(self):
        self.assertEqual(analyze("gt", 0).status, OK)

    def test_le_max_always(self):
        self.assertEqual(analyze("le", 100).status, ALWAYS)

    def test_lt_max_ok(self):
        self.assertEqual(analyze("lt", 100).status, OK)

    def test_eq_at_min_ok(self):
        self.assertEqual(analyze("eq", 0).status, OK)

    def test_eq_at_max_ok(self):
        self.assertEqual(analyze("eq", 100).status, OK)

    def test_ne_at_min_ok(self):
        self.assertEqual(analyze("ne", 0).status, OK)

    def test_epsilon_inside_ok(self):
        self.assertEqual(analyze("gt", 99.999).status, OK)
        self.assertEqual(analyze("lt", 0.001).status, OK)

    def test_epsilon_outside_never_always(self):
        self.assertEqual(analyze("gt", 100.000001).status, NEVER)
        self.assertEqual(analyze("lt", -0.000001).status, NEVER)
        self.assertEqual(analyze("ge", -0.000001).status, ALWAYS)
        self.assertEqual(analyze("le", 100.000001).status, ALWAYS)


class TestDegenerateRange(unittest.TestCase):
    def test_constant_metric_eq_always(self):
        self.assertEqual(analyze("eq", 7, metric=CONST).status, ALWAYS)

    def test_constant_metric_ne_never(self):
        self.assertEqual(analyze("ne", 7, metric=CONST).status, NEVER)

    def test_constant_metric_gt_never(self):
        self.assertEqual(analyze("gt", 7, metric=CONST).status, NEVER)

    def test_constant_metric_ge_always(self):
        self.assertEqual(analyze("ge", 7, metric=CONST).status, ALWAYS)


class TestSumBoundaries(unittest.TestCase):
    def test_sum_exact_max_gt_never(self):
        # 1h sum max = 50000 * 60 = 3,000,000
        v = analyze("gt", 3_000_000, metric=RPS, agg="sum", window=3600)
        self.assertEqual(v.status, NEVER)

    def test_sum_exact_max_ge_ok(self):
        v = analyze("ge", 3_000_000, metric=RPS, agg="sum", window=3600)
        self.assertEqual(v.status, OK)

    def test_sum_window_shorter_than_interval(self):
        # window 30s with 60s sample interval -> 0.5 sample equivalent.
        rng = agg_value_range(RPS, "sum", 30)
        self.assertEqual((rng.lo, rng.hi), (0, 25000))


class TestIntervalPrimitives(unittest.TestCase):
    def test_open_point_empty(self):
        self.assertTrue(Interval(5, 5, False, True).is_empty())
        self.assertFalse(Interval(5, 5, True, True).is_empty())

    def test_intersect_open_closed(self):
        open_at_5 = Interval(5, 10, False, True)
        closed_at_5 = Interval(0, 5, True, True)
        self.assertTrue(open_at_5.intersect(closed_at_5).is_empty())
        touching = Interval(5, 10, True, True).intersect(closed_at_5)
        self.assertFalse(touching.is_empty())
        self.assertEqual((touching.lo, touching.hi), (5, 5))

    def test_contains(self):
        full = Interval(0, 100)
        self.assertTrue(full.contains(Interval(0, 100)))
        self.assertFalse(Interval(0, 100, False, True).contains(full))
        self.assertTrue(full.contains(Interval(0, 100, False, False)))


if __name__ == "__main__":
    unittest.main()
