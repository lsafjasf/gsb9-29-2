"""框架自测（一）：用仓库内 v1/v2 两个版本样例验证矩阵结论。

这些测试锁定“正确实现”下的全部预期：
- 基线跨版本组合（EQUIV / CONVERT / REJECT）
- 块大小变化、密钥缺失、块篡改、截断四类场景
- CONVERT 转换路径真实可执行
- 样例文件可复现（重新生成与已提交文件哈希一致）
"""

import dataclasses
import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compat import fixtures, matrix
from compat.codec import Reader, Writer, KeyRing, V1, V2, IntegrityError
from compat.harness import run_case
from compat.verdicts import Verdict


class MatrixExpectationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.generate()
        cls.cells, cls.conversion = matrix.run()
        cls.pt = fixtures.sample_plaintext()

    def test_full_matrix_matches_expected(self):
        unexpected = [(c.case.scenario, c.case.producer, c.case.reader,
                       c.verdict, c.case.expected)
                      for c in self.cells if not c.matched]
        self.assertEqual(unexpected, [])

    def test_no_partial_fail_error_verdicts(self):
        bad = [c for c in self.cells
               if c.verdict in (Verdict.PARTIAL, Verdict.FAIL, Verdict.ERROR)]
        self.assertEqual(bad, [])

    def test_cell_counts(self):
        counts = {}
        for c in self.cells:
            counts[c.verdict] = counts.get(c.verdict, 0) + 1
        self.assertEqual(counts, {Verdict.EQUIV: 2, Verdict.CONVERT: 2,
                                  Verdict.REJECT: 16})

    def test_reject_cells_have_expected_reason(self):
        for c in self.cells:
            if c.case.expected == Verdict.REJECT:
                self.assertEqual(c.verdict, Verdict.REJECT)
                self.assertEqual(c.detail.get("reason"), c.case.expected_reason,
                                 f"{c.case.scenario} {c.case.producer}->{c.case.reader}")

    def test_baseline_semantics_directly(self):
        ring = fixtures.combined_ring()
        v1_blob = fixtures.load_blob(V1)
        v2_blob = fixtures.load_blob(V2)

        r1 = Reader.v1(ring).read(v1_blob)
        self.assertEqual(r1.plaintext, self.pt)
        self.assertFalse(r1.needs_conversion)

        r2_old = Reader.v2(ring).read(v1_blob)
        self.assertEqual(r2_old.plaintext, self.pt)
        self.assertTrue(r2_old.needs_conversion)

        r2_new = Reader.v2(ring).read(v2_blob)
        self.assertEqual(r2_new.plaintext, self.pt)
        self.assertFalse(r2_new.needs_conversion)

        try:
            Reader.v1(ring).read(v2_blob)
            self.fail("v1 reader must reject v2 file")
        except Exception as exc:
            self.assertEqual(exc.reason, "unsupported_version")

    def test_conversion_path_is_real(self):
        self.assertTrue(self.conversion.ok, self.conversion.evidence)

    def test_tampered_file_never_decrypts(self):
        for version in (V1, V2):
            ring = fixtures.combined_ring()
            blob = fixtures.load_scenario("tampered", version)
            with self.assertRaises(IntegrityError) as ctx:
                Reader(version, ring).read(blob)
            self.assertEqual(ctx.exception.reason, "block_mac_mismatch")

    def test_fixtures_are_reproducible(self):
        """重新生成样例必须与已提交样例逐字节一致（固定密钥/nonce/明文）。"""
        committed = {}
        for version in (V1, V2):
            with open(fixtures._paths(version)["sample"], "rb") as fh:
                committed[version] = hashlib.sha256(fh.read()).hexdigest()
        fixtures.generate(force=True)
        for version, digest in committed.items():
            with open(fixtures._paths(version)["sample"], "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
