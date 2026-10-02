"""False-positive control set: a corpus of legitimate rules
(low thresholds, long windows, tiered thresholds, sum aggregations)
must produce ZERO NEVER_FIRES / ALWAYS_FIRES / DUPLICATE_* findings,
and every planted defective rule must be caught (recall).

These tests double as the "误报数据" measurement harness.
"""

import json
import os
import unittest

from alertlint.checker import (ALWAYS_FIRES, Checker, DUP_CONTAINED,
                               DUP_EQUIVALENT, DUP_OVERLAP,
                               MISSING_METRIC, NEVER_FIRES)
from alertlint.loader import load_metrics, load_rules

HERE = os.path.dirname(__file__)
EX = os.path.join(HERE, "..", "examples")

DEFECT_TYPES = {NEVER_FIRES, ALWAYS_FIRES,
                DUP_EQUIVALENT, DUP_OVERLAP, DUP_CONTAINED}


def metrics():
    return load_metrics(os.path.join(EX, "metrics.json"))


class TestFalsePositiveControlSet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules, cls.load_errors = load_rules(
            os.path.join(EX, "legit_rules.json"))
        cls.report = Checker(metrics()).check(cls.rules)

    def test_all_rules_load(self):
        self.assertEqual(self.load_errors, [])
        self.assertEqual(len(self.rules), 14)

    def test_zero_never_or_always_false_positives(self):
        bad = [i for i in self.report.issues
               if i.type in (NEVER_FIRES, ALWAYS_FIRES)]
        self.assertEqual(bad, [], f"false positives: {bad}")

    def test_zero_duplicate_false_positives(self):
        bad = [i for i in self.report.issues
               if i.type in (DUP_EQUIVALENT, DUP_OVERLAP, DUP_CONTAINED)]
        self.assertEqual(bad, [], f"false positives: {bad}")

    def test_no_missing_metric_on_legit_corpus(self):
        bad = [i for i in self.report.issues if i.type == MISSING_METRIC]
        self.assertEqual(bad, [])

    def test_fp_rate_is_zero(self):
        false_positives = [i for i in self.report.issues
                           if i.type in DEFECT_TYPES]
        rate = len(false_positives) / len(self.rules)
        self.assertEqual(rate, 0.0)


class TestRecallControlSet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules, cls.load_errors = load_rules(
            os.path.join(EX, "bad_rules.json"))
        cls.report = Checker(metrics()).check(cls.rules)
        cls.by_rule = {}
        for issue in cls.report.issues:
            for rid in issue.rule_ids:
                cls.by_rule.setdefault(rid, []).append(issue.type)

    def test_never_fires_caught(self):
        for rid in ("B-001", "B-003", "B-005", "B-006", "B-008", "B-016"):
            self.assertIn(NEVER_FIRES, self.by_rule.get(rid, []),
                          f"{rid} missed")

    def test_always_fires_caught(self):
        for rid in ("B-002", "B-004", "B-007"):
            self.assertIn(ALWAYS_FIRES, self.by_rule.get(rid, []),
                          f"{rid} missed")

    def test_duplicates_caught(self):
        pairs = {tuple(sorted(i.rule_ids)): i.type
                 for i in self.report.issues
                 if i.type in (DUP_EQUIVALENT, DUP_OVERLAP, DUP_CONTAINED)}
        self.assertEqual(pairs.get(("B-009", "B-010")), DUP_EQUIVALENT)
        self.assertEqual(pairs.get(("B-011", "B-012")), DUP_OVERLAP)
        self.assertEqual(pairs.get(("B-013", "B-014")), DUP_CONTAINED)

    def test_missing_metric_caught(self):
        self.assertIn(MISSING_METRIC, self.by_rule.get("B-015", []))

    def test_recall_is_complete(self):
        expected = {
            "B-001", "B-002", "B-003", "B-004", "B-005", "B-006",
            "B-007", "B-008", "B-009", "B-010", "B-011", "B-012",
            "B-013", "B-014", "B-015", "B-016",
        }
        self.assertTrue(expected.issubset(set(self.by_rule)))


if __name__ == "__main__":
    unittest.main()
