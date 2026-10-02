"""mailparse 自测：功能用例 + 与 Python email（compat32）参照解析对拍。"""

from __future__ import annotations

import hashlib
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path

from mailparse import parse_message

FIXTURES = Path(__file__).parent / "fixtures"
ALL_FIXTURES = sorted(p.name for p in FIXTURES.glob("*.eml"))


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def part_by_path(root, path: str):
    for part in root.walk():
        if part.path == path:
            return part
    raise KeyError(path)


def reference_walk(data: bytes):
    """参照实现：compat32 策略下用 email 包独立解析。"""
    ref = BytesParser(policy=policy.compat32).parsebytes(data)
    nodes = list(ref.walk())
    types = [node.get_content_type() for node in nodes]
    digests = []
    for node in nodes:
        payload = node.get_payload(decode=True) or b""
        if not isinstance(payload, bytes):
            payload = b""
        digests.append(hashlib.sha256(payload).hexdigest())
    return types, digests


class NestedStructureTests(unittest.TestCase):
    def setUp(self):
        self.root = parse_message(load("nested.eml"))

    def test_paths_and_content_types(self):
        self.assertEqual(
            [(p.path, p.content_type) for p in self.root.walk()],
            [
                ("1", "multipart/mixed"),
                ("1.1", "multipart/alternative"),
                ("1.1.1", "text/plain"),
                ("1.1.2", "text/html"),
                ("1.2", "application/pdf"),
                ("1.3", "message/rfc822"),
                ("1.3.1", "text/plain"),
            ],
        )

    def test_headers_unfolded_and_decoded(self):
        self.assertEqual(self.root.headers["Subject"], "多部件嵌套邮件报告")
        self.assertEqual(self.root.headers["From"], "你好，世界 <alice@example.com>")

    def test_rfc2231_attachment_filename(self):
        attachment = part_by_path(self.root, "1.2")
        self.assertTrue(attachment.is_attachment)
        self.assertEqual(attachment.filename, "年度报告.pdf")
        self.assertEqual(attachment.params["name"], "年度报告.pdf")

    def test_cte_quoted_printable_body_restored(self):
        part = part_by_path(self.root, "1.1.1")
        self.assertEqual(part.charset_used, "utf-8")
        self.assertFalse(part.charset_fallback)
        self.assertEqual(part.text, "你好，这是纯文本正文。\n第二行。")

    def test_cte_base64_payloads_restored(self):
        html = part_by_path(self.root, "1.1.2")
        expected_html = "<html><body><p>你好，HTML 正文</p></body></html>".encode("utf-8")
        self.assertEqual(html.size, len(expected_html))
        self.assertEqual(html.sha256, hashlib.sha256(expected_html).hexdigest())

        pdf = part_by_path(self.root, "1.2")
        expected_pdf = b"%PDF-1.4 fake pdf payload\n" * 4
        self.assertEqual(pdf.size, len(expected_pdf))
        self.assertEqual(pdf.sha256, hashlib.sha256(expected_pdf).hexdigest())

    def test_embedded_message_rfc822(self):
        inner = part_by_path(self.root, "1.3.1")
        self.assertEqual(inner.headers["Subject"], "内嵌邮件")
        self.assertEqual(inner.headers["From"], "内部发件人 <inner@example.com>")
        self.assertEqual(inner.text, "这是被转发的内嵌邮件正文。")


class EncodedHeaderTests(unittest.TestCase):
    def test_mixed_charset_encoded_words_and_gbk_body(self):
        root = parse_message(load("encoded_headers.eml"))
        self.assertEqual(root.headers["Subject"], "测试头部测试")
        self.assertEqual(root.headers["From"], "张三 <zhangsan@example.com>")
        self.assertEqual(root.headers["To"], "李四 <lisi@example.com>")

        body = part_by_path(root, "1.1")
        self.assertEqual(body.charset_used, "gbk")
        self.assertEqual(body.text, "中文正文，GBK 编码。")

    def test_encoded_word_attachment_filename(self):
        root = parse_message(load("encoded_headers.eml"))
        attachment = part_by_path(root, "1.2")
        self.assertTrue(attachment.is_attachment)
        self.assertEqual(attachment.filename, "附件数据.txt")
        self.assertEqual(
            attachment.sha256,
            hashlib.sha256(b"attachment-bytes-123").hexdigest(),
        )


class CharsetFallbackTests(unittest.TestCase):
    def test_unknown_charset_falls_back_to_utf8(self):
        root = parse_message(load("unknown_charset_utf8.eml"))
        self.assertEqual(root.charset, "x-no-such-charset")
        self.assertTrue(root.charset_fallback)
        self.assertEqual(root.charset_used, "utf-8")
        self.assertEqual(root.text, "未知字符集，实为 UTF-8。\r\n")

    def test_invalid_utf8_falls_back_to_latin1(self):
        raw_body = b"caf\xe9 na\xefve \x80\x81\r\n"
        root = parse_message(load("unknown_charset_latin1.eml"))
        self.assertTrue(root.charset_fallback)
        self.assertEqual(root.charset_used, "latin-1")
        self.assertEqual(root.text.encode("latin-1"), raw_body)


class BoundaryCaseTests(unittest.TestCase):
    def test_empty_message(self):
        root = parse_message(load("empty.eml"))
        self.assertEqual([p.path for p in root.walk()], ["1"])
        self.assertEqual(root.content_type, "text/plain")
        self.assertEqual(root.size, 0)
        self.assertEqual(root.sha256, hashlib.sha256(b"").hexdigest())
        self.assertEqual(root.text, "")

    def test_headers_only(self):
        root = parse_message(load("headers_only.eml"))
        self.assertEqual(root.content_type, "text/plain")
        self.assertEqual(root.headers["Subject"], "headers only")
        self.assertEqual(root.size, 0)
        self.assertEqual(root.text, "")

    def test_boundary_like_lines_kept_in_body(self):
        root = parse_message(load("boundary_in_body.eml"))
        paths = [p.path for p in root.walk()]
        self.assertEqual(paths, ["1", "1.1", "1.2"])

        body = part_by_path(root, "1.1").text
        for line in ("--frontier42x", "-- frontier42", " --frontier42", "--frontier43--"):
            self.assertIn(line, body)
        self.assertEqual(part_by_path(root, "1.2").text, "第二个部分。")

    def test_long_folded_headers(self):
        root = parse_message(load("long_header.eml"))
        subject = root.headers["Subject"]
        comment = root.headers["X-Comment"]
        self.assertEqual(len(subject), 4800)
        self.assertGreaterEqual(len(comment), 8000)
        self.assertNotIn("\n", subject)
        self.assertNotIn("\n", comment)
        self.assertEqual(subject[:4], "超长标题")


class DifferentialTests(unittest.TestCase):
    """逐固件与 compat32 参照实现对拍：层级类型序列与各部分摘要一致。"""

    def test_structure_and_digests_match_reference(self):
        for name in ALL_FIXTURES:
            with self.subTest(fixture=name):
                data = load(name)
                root = parse_message(data)

                our_types = [p.content_type for p in root.walk()]
                our_digests = [p.sha256 for p in root.walk()]
                ref_types, ref_digests = reference_walk(data)

                self.assertEqual(our_types, ref_types)
                self.assertEqual(our_digests, ref_digests)


if __name__ == "__main__":
    unittest.main()
