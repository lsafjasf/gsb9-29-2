"""Duplicate / overlap detection, including false-positive guards
(tiered thresholds must NOT be flagged)."""

import unittest

from alertlint.duplicates import find_duplicates, pair_similarity
from alertlint.loader import load_metrics, load_rules
from alertlint.model import Condition, MetricMeta, Rule
import os

HERE = os.path.dirname(__file__)
EX = os.path.join(HERE, "..", "examples")


def reg():
    return {m.name: m for m in load_metrics(os.path.join(EX, "metrics.json"))}


def cpu_rule(rid, threshold, op="gt", window=300, agg="avg"):
    return Rule(id=rid, name=rid, conditions=[
        Condition("cpu_usage_percent", op, threshold, agg, window)])


def pair_ids(findings):
    return {tuple(sorted((a.id, b.id))) for a, b, _ in findings}


class TestEquivalence(unittest.TestCase):
    def test_exact_copy_equivalent(self):
        r1 = cpu_rule("A", 90)
        r2 = cpu_rule("B", 90)
        sim = pair_similarity(r1, r2, reg())
        self.assertEqual(sim.kind, "equivalent")
        self.assertEqual(sim.score, 1.0)

    def test_reordered_and_conditions_equivalent(self):
        def build(rid, order):
            return Rule(id=rid, name=rid, combinator="all", conditions=[
                Condition(n, "gt", t, "avg", 300)
                for n, t in order])
        r1 = build("A", [("cpu_usage_percent", 90), ("memory_usage_percent", 80)])
        r2 = build("B", [("memory_usage_percent", 80), ("cpu_usage_percent", 90)])
        sim = pair_similarity(r1, r2, reg())
        self.assertEqual(sim.kind, "equivalent")

    def test_single_condition_combinator_agnostic(self):
        r1 = Rule(id="A", name="A", combinator="all",
                  conditions=[Condition("cpu_usage_percent", "gt", 90, "avg", 300)])
        r2 = Rule(id="B", name="B", combinator="any",
                  conditions=[Condition("cpu_usage_percent", "gt", 90, "avg", 300)])
        self.assertEqual(pair_similarity(r1, r2, reg()).kind, "equivalent")


class TestOverlap(unittest.TestCase):
    def test_near_duplicate_score_is_jaccard(self):
        r1 = cpu_rule("A", 90)  # (90,100] measure 10
        r2 = cpu_rule("B", 92)  # (92,100] measure 8
        sim = pair_similarity(r1, r2, reg())
        self.assertAlmostEqual(sim.score, 0.8, places=6)
        self.assertEqual(sim.containment, 1.0)

    def test_near_duplicate_flagged(self):
        findings = find_duplicates([cpu_rule("A", 90), cpu_rule("B", 92)], reg())
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0][2].kind, "overlap")

    def test_different_window_not_same_series(self):
        r1 = cpu_rule("A", 90, window=300)
        r2 = cpu_rule("B", 90, window=600)
        self.assertEqual(pair_similarity(r1, r2, reg()).kind, "none")

    def test_different_metric_not_duplicate(self):
        r1 = cpu_rule("A", 90)
        r2 = Rule(id="B", name="B", conditions=[
            Condition("memory_usage_percent", "gt", 90, "avg", 300)])
        self.assertEqual(pair_similarity(r1, r2, reg()).kind, "none")

    def test_different_agg_not_same_series(self):
        r1 = cpu_rule("A", 90, agg="avg")
        r2 = cpu_rule("B", 90, agg="max")
        self.assertEqual(pair_similarity(r1, r2, reg()).kind, "none")

    def test_tiered_thresholds_not_flagged(self):
        # warning >90 vs critical >99: containment but Jaccard only 0.1
        findings = find_duplicates([cpu_rule("warn", 90), cpu_rule("crit", 99)],
                                   reg(), threshold=0.8)
        self.assertEqual(findings, [])

    def test_contained_rule_flagged_as_info(self):
        r1 = cpu_rule("loose", 90)
        r2 = Rule(id="strict", name="strict", combinator="all", conditions=[
            Condition("cpu_usage_percent", "gt", 90, "avg", 300),
            Condition("memory_usage_percent", "gt", 90, "avg", 300)])
        findings = find_duplicates([r1, r2], reg())
        self.assertEqual(pair_ids(findings), {("loose", "strict")})
        self.assertEqual(findings[0][2].kind, "contained")
        self.assertAlmostEqual(findings[0][2].score, 0.5)

    def test_same_conditions_opposite_combinator_flagged(self):
        def build(rid, combinator):
            return Rule(id=rid, name=rid, combinator=combinator, conditions=[
                Condition("cpu_usage_percent", "gt", 90, "avg", 300),
                Condition("memory_usage_percent", "gt", 90, "avg", 300)])
        sim = pair_similarity(build("A", "all"), build("B", "any"), reg())
        self.assertEqual(sim.kind, "overlap")
        self.assertGreaterEqual(sim.score, 0.8)


class TestCorpusPairs(unittest.TestCase):
    def test_example_file_planted_pairs_found(self):
        rules, _ = load_rules(os.path.join(EX, "rules.json"))
        findings = find_duplicates(rules, reg())
        ids = pair_ids(findings)
        self.assertIn(tuple(sorted(("R-005", "R-006"))), ids)      # exact
        self.assertIn(tuple(sorted(("R-005", "R-007"))), ids)      # near
        self.assertIn(tuple(sorted(("R-001", "R-017"))), ids)      # contained


if __name__ == "__main__":
    unittest.main()
