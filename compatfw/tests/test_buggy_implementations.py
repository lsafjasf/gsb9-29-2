"""框架自测（二）：对错误实现必须报失败。

注入三类有真实危害的错误读取实现，要求框架分别稳定抓到：

1. SilentTruncateReader  静默截断：解密成功但丢弃超长部分 -> FAIL（内容不等价）
2. PartialReadReader     先返回部分明文再抛异常          -> PARTIAL（永不通过）
3. LenientMacReader      块 HMAC 不匹配仍返回明文        -> FAIL（篡改可读）
4. CrashyReader          抛出非约定异常（裸 RuntimeError）-> ERROR（实现缺陷）

另含判定器元测试：任何一格结论与期望不符，矩阵级检查必须失败。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compat import fixtures, matrix
from compat.codec import V1, V2, IntegrityError
from compat.harness import Case, Verdict, run_case
from compat.verdicts import Verdict as V


class _BaseBuggy:
    """错误读取器公共骨架：复用真实 codec 的部分解析，但在关键点出错。"""

    def __init__(self, real_reader):
        self._real = real_reader
        self.version = real_reader.version


class SilentTruncateReader(_BaseBuggy):
    """块大小声明变化时，静默只取前 n 字节而不拒绝。"""

    def read(self, blob):
        result = self._real.read(blob)
        return _Result(result.plaintext[: len(result.plaintext) - 1],
                       result.needs_conversion, result.version)


class PartialReadReader(_BaseBuggy):
    """读到第 2 个块失败时，把第 1 个块的明文随异常暴露出去。"""

    def read(self, blob):
        result = self._real.read(blob)
        exc = IntegrityError("simulated late failure", "block_mac_mismatch")
        exc.partial_plaintext = result.plaintext[:1024]  # 部分内容外泄
        raise exc


class LenientMacReader(_BaseBuggy):
    """块 HMAC 校验失败时仅打印警告并返回解出的明文（危险实现）。"""

    def read(self, blob):
        try:
            return self._real.read(blob)
        except IntegrityError as exc:
            if exc.reason != "block_mac_mismatch":
                raise
            # 错误做法：吞掉鉴权失败，返回未经验证的“明文”
            return _FakePlaintext(self._real, blob)


class _FakePlaintext:
    """模拟宽松实现：跳过块 HMAC，直接解密返回（内容可能已被篡改）。"""

    def __init__(self, real_reader, blob):
        from compat.codec import (
            _HDR_V1, _HDR_V2, header_len, _BLOCK_HDR, MAC_LEN, _derive
        )
        from compat import crypto
        hlen = header_len(real_reader.version if blob[7] == 1 else 2)
        version = blob[7]
        if version == V1:
            _, _, kv, bs, bc = _HDR_V1.unpack(blob[:hlen])
            nonce = b"\x00" * 16
        else:
            _, _, kv, bs, bc, nonce = _HDR_V2.unpack(blob[:hlen])
        master = real_reader.keys.get(kv)
        _, enc_key, _ = _derive(version, kv, master)
        pos = hlen + MAC_LEN
        pieces = []
        for i in range(bc):
            idx, length = _BLOCK_HDR.unpack(blob[pos:pos + 16])
            pos += 16
            cipher = blob[pos:pos + length]
            pos += length + MAC_LEN
            pieces.append(crypto.xor_keystream(enc_key, nonce, i, bytes(cipher)))
        self.plaintext = b"".join(pieces)
        self.needs_conversion = False
        self.version = version
        self.block_size = bs
        self.block_count = bc


class CrashyReader:
    """抛出非框架约定的异常类型——读取实现自身有 bug。"""

    version = 2

    def read(self, blob):
        raise RuntimeError("segfault-like internal error")


class _Result:
    def __init__(self, plaintext, needs_conversion, version):
        self.plaintext = plaintext
        self.needs_conversion = needs_conversion
        self.version = version


def _make_case(reader_impl, scenario="baseline", file_version=V1,
               reader_version=V2, expected=V.CONVERT,
               blob_loader=None):
    pt = fixtures.sample_plaintext()
    if blob_loader is None:
        blob_loader = lambda: fixtures.load_blob(file_version)  # noqa: E731
    return Case(
        scenario=scenario,
        producer=f"W{file_version}",
        reader=f"R{reader_version}",
        expected=expected,
        build_blob=blob_loader,
        reader_impl=reader_impl,
        expected_plaintext=pt,
    )


class BuggyImplementationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.generate()
        cls.pt = fixtures.sample_plaintext()

    def test_silent_truncation_must_fail(self):
        from compat.codec import Reader
        buggy = SilentTruncateReader(Reader.v2(fixtures.combined_ring()))
        cell = run_case(_make_case(buggy))
        self.assertEqual(cell.verdict, Verdict.FAIL)
        self.assertFalse(cell.matched)
        self.assertIn("内容不等价", cell.evidence)

    def test_partial_read_is_never_pass(self):
        from compat.codec import Reader
        buggy = PartialReadReader(Reader.v2(fixtures.combined_ring()))
        cell = run_case(_make_case(buggy))
        self.assertEqual(cell.verdict, Verdict.PARTIAL)
        self.assertFalse(cell.matched)
        self.assertIn("不算通过", cell.evidence)

    def test_lenient_mac_on_tampered_file_must_fail(self):
        from compat.codec import Reader
        buggy = LenientMacReader(Reader.v2(fixtures.combined_ring()))
        case = _make_case(
            buggy, scenario="block_tampered", file_version=V2, reader_version=V2,
            expected=Verdict.REJECT,
            blob_loader=lambda: fixtures.load_scenario("tampered", V2),
        )
        case.expected_reason = "block_mac_mismatch"
        cell = run_case(case)
        self.assertIn(cell.verdict, (Verdict.FAIL, Verdict.PARTIAL))
        self.assertFalse(cell.matched)

    def test_non_conforming_exception_is_error(self):
        buggy = CrashyReader()
        cell = run_case(_make_case(buggy))
        self.assertEqual(cell.verdict, Verdict.ERROR)
        self.assertFalse(cell.matched)
        self.assertIn("RuntimeError", cell.evidence)

    def test_correct_implementation_still_passes(self):
        """对照组：真实 R2 读 v1 必须是 CONVERT，确保上面的失败确实来自错误实现。"""
        from compat.codec import Reader
        cell = run_case(_make_case(Reader.v2(fixtures.combined_ring())))
        self.assertEqual(cell.verdict, Verdict.CONVERT)
        self.assertTrue(cell.matched)

    def test_any_unexpected_cell_fails_matrix_run(self):
        """矩阵级断言：20 格中只要有一格 matched=False，整体检查即失败。"""
        cells, conversion = matrix.run()
        self.assertEqual(len(cells), 20)
        self.assertTrue(all(c.matched for c in cells))
        self.assertTrue(conversion.ok)

        # 人为破坏一个单元格的 matched 标记，验证聚合检查会变红
        cells[0] = dataclasses_replace(cells[0], matched=False)
        self.assertFalse(all(c.matched for c in cells))


def dataclasses_replace(obj, **kw):
    import dataclasses
    return dataclasses.replace(obj, **kw)


if __name__ == "__main__":
    unittest.main()
