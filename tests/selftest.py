"""框架自测（标准库 unittest）。

运行方式：
    python3 tests/selftest.py
    python3 -m unittest tests.selftest -v
"""
from __future__ import annotations

import os
import random
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "examples"))

from difftest import harness
from difftest.campaign import Campaign, DiffConfig
from difftest.generator import EDGE_KINDS, Generator
from difftest.reducer import shrink

import toy_adapter


def signature_on(data):
    a = harness.run_parser(toy_adapter.parse_a, data)
    b = harness.run_parser(toy_adapter.parse_b, data)
    return harness.signature(harness.classify(a, b), a, b), a, b


class TestClassification(unittest.TestCase):
    """三类必须区分的结论，外加同成功但值不同。"""

    def test_match_success(self):
        a = harness.Outcome(True, {"x": 1}, None)
        b = harness.Outcome(True, {"x": 1}, None)
        self.assertEqual(harness.classify(a, b), harness.MATCH)

    def test_match_same_error_category(self):
        a = harness.Outcome(False, None, "BadMagic")
        b = harness.Outcome(False, None, "BadMagic")
        self.assertEqual(harness.classify(a, b), harness.MATCH)

    def test_error_mismatch(self):
        a = harness.Outcome(False, None, "BadUintSize")
        b = harness.Outcome(False, None, "DepthExceeded")
        self.assertEqual(harness.classify(a, b), harness.ERROR_MISMATCH)

    def test_outcome_mismatch_either_side(self):
        ok = harness.Outcome(True, [], None)
        err = harness.Outcome(False, None, "EmptyString")
        self.assertEqual(harness.classify(ok, err), harness.OUTCOME_MISMATCH)
        self.assertEqual(harness.classify(err, ok), harness.OUTCOME_MISMATCH)

    def test_value_mismatch(self):
        a = harness.Outcome(True, ["uint", 1], None)
        b = harness.Outcome(True, ["uint", 2], None)
        self.assertEqual(harness.classify(a, b), harness.VALUE_MISMATCH)


class TestGenerator(unittest.TestCase):
    def test_four_edge_cases_first_and_present(self):
        gen = Generator(toy_adapter.SPEC, DiffConfig(), seed=7)
        cases = list(gen.cases(len(EDGE_KINDS)))
        self.assertEqual([c.meta["edge"] for c in cases], list(EDGE_KINDS))
        self.assertEqual(cases[0].data, b"")                       # 空报文
        self.assertGreater(len(cases[1].data), toy_adapter.MAX_SIZE)  # 超大
        self.assertGreater(len(cases[2].data), 2 * 8)             # 深度极大
        self.assertTrue(all(c.data is not None for c in cases))

    def test_strategies_tagged(self):
        gen = Generator(toy_adapter.SPEC, DiffConfig(), seed=11)
        tags = {c.tag for c in list(gen.cases(300))[4:]}
        for strategy in (
            "random_tree",
            "length_boundary",
            "field_mutation",
            "nesting",
            "illegal_sequence",
        ):
            self.assertIn(strategy, tags)

    def test_deterministic_with_seed(self):
        seq_a = [c.data for c in Generator(toy_adapter.SPEC, DiffConfig(), 42).cases(80)]
        seq_b = [c.data for c in Generator(toy_adapter.SPEC, DiffConfig(), 42).cases(80)]
        self.assertEqual(seq_a, seq_b)


class TestReducer(unittest.TestCase):
    def test_shrinks_and_preserves_signature(self):
        # 深度 6：A（限深 8）成功，B（限深 4）DepthExceeded
        node = ("uint", 1, 5)
        for _ in range(5):
            node = ("group", [node])
        data = toy_adapter.SPEC.serialize([node])

        sig, a, b = signature_on(data)
        self.assertEqual(sig[0], harness.OUTCOME_MISMATCH)

        pred = lambda d: signature_on(d)[0] == sig
        shrunk = shrink(data, pred)

        self.assertTrue(pred(shrunk))
        self.assertLess(len(shrunk), len(data))

    def test_requires_predicate_on_original(self):
        with self.assertRaises(ValueError):
            shrink(b"\xab", lambda _: False)


class TestCampaignEndToEnd(unittest.TestCase):
    def test_finds_planted_diffs_and_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = DiffConfig(cases=400, seed=2024, outdir=tmp)
            report = Campaign(
                toy_adapter.SPEC, toy_adapter.parse_a, toy_adapter.parse_b, cfg
            ).run()

            categories = {m["category"] for m in report["mismatches"]}
            self.assertIn(harness.OUTCOME_MISMATCH, categories)
            self.assertIn(harness.ERROR_MISMATCH, categories)

            for m in report["mismatches"]:
                self.assertLessEqual(m["minimized_len"], m["original_len"])
                self.assertTrue(os.path.exists(os.path.join(tmp, m["minimized_file"])))
                self.assertTrue(os.path.exists(os.path.join(tmp, m["original_file"])))
                # 最小反例文件可被两个解析器重新复现相同类别
                path = os.path.join(tmp, m["minimized_file"])
                with open(path, "rb") as fh:
                    payload = fh.read()
                sig, _, _ = signature_on(payload)
                self.assertEqual(sig[0], m["category"])

            self.assertTrue(os.path.exists(os.path.join(tmp, "report.json")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "report.txt")))

            cov = report["coverage"]
            for kind in EDGE_KINDS:  # 空/超大/极深/全非法全部覆盖
                self.assertGreaterEqual(cov["edge_counts"].get(kind, 0), 1)
            self.assertGreater(cov["max_size_generated"], toy_adapter.MAX_SIZE)

    def test_hints_when_edge_cases_missing(self):
        class NoEdgeSpec(toy_adapter.ToySpec):
            def edge_case(self, kind, rng, cfg):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            cfg = DiffConfig(cases=20, seed=5, outdir=tmp)
            report = Campaign(
                NoEdgeSpec(), toy_adapter.parse_a, toy_adapter.parse_b, cfg
            ).run()
            hints = " ".join(report["hints"])
            for label in ("空报文", "超大报文", "深度极大", "全部非法"):
                self.assertIn(label, hints)


if __name__ == "__main__":
    unittest.main(verbosity=2)
