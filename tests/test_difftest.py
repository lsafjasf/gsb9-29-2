"""差分框架自测：生成器、分类、归约、覆盖率、端到端。"""

import json
import os
import shutil
import tempfile
import unittest

from difftest.cli import main as cli_main
from difftest.coverage import Coverage
from difftest.errors import Category, ParseError
from difftest.generator import Generator, _record
from difftest.reducer import Reducer
from difftest.runner import (DifferentialRunner, Verdict, classify, run_one)
from parsers import candidate, reference

T_NODE, T_STR, T_UINT = 0x01, 0x02, 0x03


def chain(depth):
    payload = _record(T_STR, b"")
    for _ in range(depth):
        payload = _record(T_NODE, payload)
    return payload


class TestParsers(unittest.TestCase):
    def test_reference_parses_valid(self):
        data = _record(T_STR, "中文".encode()) + _record(T_UINT, b"\x01\x00")
        result = reference.parse(data)
        self.assertEqual(result[0], {"type": "str", "value": "中文"})
        self.assertEqual(result[1], {"type": "uint", "value": 256})

    def test_reference_error_categories(self):
        with self.assertRaises(ParseError) as ctx:
            reference.parse(b"\x02\x00")
        self.assertEqual(ctx.exception.category, Category.TRUNCATED_HEADER)
        with self.assertRaises(ParseError) as ctx:
            reference.parse(b"\x02\x00\x05ab")
        self.assertEqual(ctx.exception.category, Category.TRUNCATED_PAYLOAD)
        with self.assertRaises(ParseError) as ctx:
            reference.parse(b"\x7f\x00\x00")
        self.assertEqual(ctx.exception.category, Category.UNKNOWN_TYPE)
        with self.assertRaises(ParseError) as ctx:
            reference.parse(_record(T_STR, b"\xff\xfe"))
        self.assertEqual(ctx.exception.category, Category.INVALID_UTF8)

    def test_injected_divergences_exist(self):
        # 深度恰好 32：参照接受，候选拒绝（注入差异 1）
        data = chain(31)  # 31 层 NODE -> 最内层序列深度 32
        reference.parse(data)
        with self.assertRaises(ParseError):
            candidate.parse(data)
        # 8 字节 UINT：参照接受，候选拒绝（注入差异 2）
        data = _record(T_UINT, b"\x00" * 8)
        reference.parse(data)
        with self.assertRaises(ParseError):
            candidate.parse(data)


class TestClassify(unittest.TestCase):
    def ok(self, value):
        return run_one(lambda d: value, b"")

    def err(self, category):
        def parse(_):
            raise ParseError(category)
        return run_one(parse, b"")

    def test_match(self):
        self.assertEqual(classify(self.ok([1]), self.ok([1])), Verdict.MATCH)

    def test_result_mismatch(self):
        self.assertEqual(classify(self.ok([1]), self.ok([2])),
                         Verdict.RESULT_MISMATCH)

    def test_both_fail_same(self):
        self.assertEqual(classify(self.err(Category.UNKNOWN_TYPE),
                                  self.err(Category.UNKNOWN_TYPE)),
                         Verdict.BOTH_FAIL_SAME)

    def test_error_category_mismatch(self):
        self.assertEqual(classify(self.err(Category.UNKNOWN_TYPE),
                                  self.err(Category.TRUNCATED_HEADER)),
                         Verdict.ERROR_CATEGORY_MISMATCH)

    def test_one_sided(self):
        self.assertEqual(classify(self.ok([]), self.err(Category.UNKNOWN_TYPE)),
                         Verdict.ONE_SIDED)
        self.assertEqual(classify(self.err(Category.UNKNOWN_TYPE), self.ok([])),
                         Verdict.ONE_SIDED)

    def test_unexpected_exception_is_categorized(self):
        def boom(_):
            raise RuntimeError("crash")
        out = run_one(boom, b"x")
        self.assertEqual(out.status, "error")
        self.assertEqual(out.category, "UNEXPECTED:RuntimeError")


class TestGenerator(unittest.TestCase):
    def test_deterministic_with_seed(self):
        a = [d for d, _ in Generator(seed=42).cases(50)]
        b = [d for d, _ in Generator(seed=42).cases(50)]
        self.assertEqual(a, b)

    def test_fixed_edge_cases_first(self):
        cases = list(Generator(seed=1).cases(5))
        tags = [set(t) for _, t in cases]
        self.assertEqual(cases[0][0], b"")
        self.assertIn("empty", tags[0])
        self.assertIn("oversized", tags[1])
        self.assertGreater(len(cases[1][0]), 70000)
        self.assertIn("deep_nesting", tags[2])
        self.assertIn("all_illegal", tags[4])

    def test_random_cases_cover_features(self):
        cov = Coverage()
        for data, tags in Generator(seed=7).cases(500):
            cov.record(tags)
        rep = cov.report()
        self.assertEqual(rep["missing"], [],
                         "500 个用例应覆盖全部特性: %s" % rep["missing"])
        self.assertEqual(rep["percent"], 100.0)


class TestReducer(unittest.TestCase):
    def setUp(self):
        self.reducer = Reducer(reference.parse, candidate.parse)

    def test_reduces_uint8_divergence_to_single_record(self):
        noise = _record(T_STR, b"hello") + _record(T_UINT, b"\x01")
        data = noise + _record(T_UINT, b"\x00" * 8) + noise
        minimized, out_a, out_b = self.reducer.reduce(data)
        self.assertEqual(minimized, _record(T_UINT, b"\x00" * 8))
        self.assertEqual(out_a.status, "ok")
        self.assertEqual(out_b.category, Category.UINT_BAD_LENGTH)

    def test_reduces_depth_divergence(self):
        data = _record(T_STR, b"pad") + chain(31)
        minimized, out_a, out_b = self.reducer.reduce(data)
        self.assertEqual(minimized, chain(31))
        self.assertEqual(out_a.status, "ok")
        self.assertEqual(out_b.category, Category.DEPTH_EXCEEDED)

    def test_minimized_is_minimal_wrt_record_removal(self):
        data = chain(31)
        minimized, _, _ = self.reducer.reduce(data)
        # 再删任何一条记录都会破坏差异（深度不足）
        spans = Reducer._spans(minimized)
        for start, end in spans:
            cand = minimized[:start] + minimized[end:]
            out_a = run_one(reference.parse, cand)
            out_b = run_one(candidate.parse, cand)
            self.assertNotEqual(
                (classify(out_a, out_b), out_a.category, out_b.category),
                (Verdict.ONE_SIDED, None, Category.DEPTH_EXCEEDED))

    def test_preserves_error_category_mismatch_signature(self):
        # 构造"两边都失败但类别不同"：参照 UNKNOWN_TYPE，候选 DEPTH_EXCEEDED
        # 深度 32 的 NODE 外壳里放一个未知类型记录
        inner = b"\x7f\x00\x00"
        payload = inner
        for _ in range(31):
            payload = _record(T_NODE, payload)
        minimized, out_a, out_b = self.reducer.reduce(payload)
        self.assertEqual(out_a.category, Category.UNKNOWN_TYPE)
        self.assertEqual(out_b.category, Category.DEPTH_EXCEEDED)
        self.assertLessEqual(len(minimized), len(payload))


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="difftest_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cli_finds_injected_divergences(self):
        rc = cli_main(["--cases", "400", "--seed", "3", "--out", self.tmp])
        self.assertEqual(rc, 1, "注入差异应被检出")
        report_path = os.path.join(self.tmp, "diff_report.json")
        with open(report_path, encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertGreater(report["summary"]["divergent"], 0)
        self.assertEqual(report["coverage"]["percent"], 100.0)
        self.assertTrue(report["divergences"])
        entry = report["divergences"][0]
        # 最小反例落盘且两边输出都记录
        self.assertIn("minimized", entry)
        self.assertIn("parsers.reference:parse", entry["outputs"])
        self.assertIn("parsers.candidate:parse", entry["outputs"])
        min_path = os.path.join(self.tmp, entry["minimized"]["path"])
        with open(min_path, "rb") as fh:
            self.assertEqual(fh.read().hex(), entry["minimized"]["hex"])
        # 注入的两类差异都应出现
        kinds = {(d["verdict"],
                  d["outputs"]["parsers.candidate:parse"].get("category"))
                 for d in report["divergences"]}
        self.assertIn((Verdict.ONE_SIDED, Category.UINT_BAD_LENGTH), kinds)
        self.assertIn((Verdict.ONE_SIDED, Category.DEPTH_EXCEEDED), kinds)

    def test_identical_implementations_no_divergence(self):
        rc = cli_main(["--cases", "200", "--seed", "5", "--out", self.tmp,
                       "--impl-a", "parsers.reference:parse",
                       "--impl-b", "parsers.reference:parse"])
        self.assertEqual(rc, 0)

    def test_runner_counts_verdicts(self):
        runner = DifferentialRunner(reference.parse, candidate.parse)
        runner.run_case(0, _record(T_UINT, b"\x00" * 8))
        runner.run_case(1, _record(T_STR, b"ok"))
        runner.run_case(2, b"\x7f\x00\x00")
        summary = runner.summary()
        self.assertEqual(summary["verdicts"][Verdict.ONE_SIDED], 1)
        self.assertEqual(summary["verdicts"][Verdict.MATCH], 1)
        self.assertEqual(summary["verdicts"][Verdict.BOTH_FAIL_SAME], 1)


if __name__ == "__main__":
    unittest.main()
