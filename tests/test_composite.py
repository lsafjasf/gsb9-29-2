"""Composite (AND/OR) conditions and missing-metric behaviour."""

import unittest

from alertlint.analysis import ALWAYS, NEVER, OK, UNKNOWN, analyze_rule
from alertlint.model import Condition, MetricMeta, Rule

CPU = MetricMeta("cpu", 0, 100, sample_interval_seconds=60)
MEM = MetricMeta("mem", 0, 100, sample_interval_seconds=60)
REG = {m.name: m for m in (CPU, MEM)}


def cond(metric="cpu", op="gt", threshold=90, window=300):
    return Condition(metric=metric, op=op, threshold=threshold,
                     agg="avg", window_seconds=window)


def rule(conds, combinator="all"):
    return Rule(id="T", name="T", conditions=conds, combinator=combinator)


class TestAnd(unittest.TestCase):
    def test_contradiction_same_metric(self):
        r = rule([cond(op="gt", threshold=80), cond(op="lt", threshold=20)])
        a = analyze_rule(r, REG)
        self.assertEqual(a.status, NEVER)
        self.assertIn("AND-contradiction", a.reasons[0])

    def test_boundary_contradiction(self):
        # x >= 90 AND x <= 90 is satisfiable (exactly 90) -> NOT never.
        r = rule([cond(op="ge", threshold=90), cond(op="le", threshold=90)])
        self.assertEqual(analyze_rule(r, REG).status, OK)

    def test_open_boundary_contradiction(self):
        # x > 90 AND x <= 90 is empty.
        r = rule([cond(op="gt", threshold=90), cond(op="le", threshold=90)])
        self.assertEqual(analyze_rule(r, REG).status, NEVER)

    def test_tautological_and(self):
        r = rule([cond(op="ge", threshold=0), cond(op="le", threshold=100)])
        self.assertEqual(analyze_rule(r, REG).status, ALWAYS)

    def test_independent_metrics_ok(self):
        r = rule([cond(metric="cpu", op="gt", threshold=95),
                  cond(metric="mem", op="gt", threshold=95)])
        self.assertEqual(analyze_rule(r, REG).status, OK)

    def test_and_with_one_impossible_branch(self):
        r = rule([cond(metric="cpu", op="gt", threshold=150),
                  cond(metric="mem", op="gt", threshold=50)])
        self.assertEqual(analyze_rule(r, REG).status, NEVER)


class TestOr(unittest.TestCase):
    def test_covering_or_always(self):
        r = rule([cond(op="gt", threshold=60), cond(op="le", threshold=60)],
                 combinator="any")
        self.assertEqual(analyze_rule(r, REG).status, ALWAYS)

    def test_open_gap_or_not_always(self):
        # x > 60 OR x < 60: silent exactly at 60 -> not always.
        r = rule([cond(op="gt", threshold=60), cond(op="lt", threshold=60)],
                 combinator="any")
        self.assertEqual(analyze_rule(r, REG).status, OK)

    def test_or_never(self):
        r = rule([cond(op="gt", threshold=200), cond(op="lt", threshold=-5)],
                 combinator="any")
        self.assertEqual(analyze_rule(r, REG).status, NEVER)

    def test_or_partially_feasible_ok(self):
        r = rule([cond(op="gt", threshold=200), cond(op="gt", threshold=90)],
                 combinator="any")
        self.assertEqual(analyze_rule(r, REG).status, OK)

    def test_or_across_metrics_not_always(self):
        # cpu>90 OR mem>90 cannot be proven always (independent metrics).
        r = rule([cond(metric="cpu", op="gt", threshold=90),
                  cond(metric="mem", op="gt", threshold=90)], combinator="any")
        self.assertEqual(analyze_rule(r, REG).status, OK)


class TestMissingMetric(unittest.TestCase):
    def test_missing_metric_rule_unknown(self):
        r = rule([cond(metric="ghost", op="gt", threshold=1)])
        a = analyze_rule(r, REG)
        self.assertEqual(a.status, UNKNOWN)
        self.assertNotEqual(a.status, NEVER)
        self.assertNotEqual(a.status, ALWAYS)

    def test_and_with_missing_metric_unknown(self):
        r = rule([cond(metric="cpu", op="gt", threshold=90),
                  cond(metric="ghost", op="gt", threshold=1)])
        self.assertEqual(analyze_rule(r, REG).status, UNKNOWN)

    def test_and_missing_metric_but_proven_never(self):
        # A proven contradiction still wins over the unknown branch.
        r = rule([cond(metric="cpu", op="gt", threshold=150),
                  cond(metric="ghost", op="gt", threshold=1)])
        self.assertEqual(analyze_rule(r, REG).status, NEVER)

    def test_or_all_missing_unknown(self):
        r = rule([cond(metric="ghost1", op="gt", threshold=1),
                  cond(metric="ghost2", op="lt", threshold=1)], combinator="any")
        self.assertEqual(analyze_rule(r, REG).status, UNKNOWN)


if __name__ == "__main__":
    unittest.main()
