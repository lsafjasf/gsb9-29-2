# -*- coding: utf-8 -*-
"""mime_parser 单元自测：编码还原、降级策略与边界情形。"""
import hashlib
import unittest

from corpus import (
    BOUNDARY_LIKE_BODY, BOUNDARY_PREFIX_IN_BODY, ENCODED_HEADERS, LONG_HEADER,
    MESSAGE_RFC822, NESTED_MIXED, QP_SOFTPREAK, RFC2231_FILENAME,
    UNKNOWN_CHARSET_BODY,
)
from mime_parser import (
    decode_header_value, decode_text, dump_tree, parse_message,
    parse_structured_header,
)


class TestEmptyAndHeadersOnly(unittest.TestCase):
    def test_empty_message(self):
        root = parse_message(b"")
        self.assertEqual(root.content_type, "text/plain")
        self.assertEqual(root.payload, b"")
        self.assertFalse(root.children)

    def test_headers_only_no_blank_line(self):
        root = parse_message(b"Subject: no body\r\nFrom: a@example.com\r\n")
        self.assertEqual(root.header("subject"), "no body")
        self.assertEqual(root.payload, b"")

    def test_headers_and_blank_line(self):
        root = parse_message(b"Subject: x\r\n\r\n")
        self.assertEqual(root.payload, b"")


class TestHeaderFolding(unittest.TestCase):
    def test_folded_header_joined(self):
        root = parse_message(b"X-A: one\r\n\ttwo\r\n  three\r\n\r\n")
        self.assertIn("one", root.header("x-a"))
        self.assertIn("two", root.header("x-a"))
        self.assertIn("three", root.header("x-a"))

    def test_long_header(self):
        root = parse_message(LONG_HEADER)
        value = root.header("x-long-header")
        self.assertIsNotNone(value)
        self.assertGreater(len(value), 11000)
        self.assertEqual(value.count("x"), len(value.replace(" ", "")))


class TestEncodedWords(unittest.TestCase):
    def test_base64_and_q_encoded_words(self):
        self.assertEqual(
            decode_header_value("=?UTF-8?B?5L2g5aW977yM5LiW55WM?="),
            "你好，世界",
        )
        self.assertEqual(
            decode_header_value("=?GB2312?Q?=C4=FA=BA=C3?="),
            "您好",
        )

    def test_adjacent_words_whitespace_dropped(self):
        root = parse_message(ENCODED_HEADERS)
        self.assertEqual(root.decoded_header("subject"),
                         "你好，世界您好 plain tail")

    def test_unknown_charset_encoded_word_falls_back(self):
        root = parse_message(ENCODED_HEADERS)
        # =?X-NO-SUCH-CHARSET?B?...?= 的载荷是合法 UTF-8，应走 utf-8 降级
        self.assertEqual(root.decoded_header("x-unknown-word"),
                         "这是测试")


class TestParams(unittest.TestCase):
    def test_rfc2231_continuation(self):
        root = parse_message(RFC2231_FILENAME)
        attachment = root.children[0]
        self.assertEqual(attachment.disp_params["filename"],
                         "中文文件名字 很长.bin")

    def test_quoted_param_escape(self):
        root = parse_message(RFC2231_FILENAME)
        self.assertEqual(root.children[1].disp_params["filename"], 'quo"ted.txt')

    def test_rfc2231_single_encoded(self):
        main, params = parse_structured_header(
            "attachment; filename*=utf-8''%E6%8A%A5%E5%91%8A.pdf")
        self.assertEqual(params["filename"], "报告.pdf")


class TestMultipart(unittest.TestCase):
    def test_nested_paths_and_types(self):
        root = parse_message(NESTED_MIXED)
        got = [(p.path, p.content_type) for p in root.walk()]
        self.assertEqual(got, [
            ("1", "multipart/mixed"),
            ("1.1", "text/plain"),
            ("1.2", "multipart/alternative"),
            ("1.2.1", "text/plain"),
            ("1.2.2", "text/html"),
            ("1.3", "application/pdf"),
        ])

    def test_attachment_payload_and_digest(self):
        root = parse_message(NESTED_MIXED)
        pdf = root.children[2]
        self.assertTrue(pdf.is_attachment)
        self.assertEqual(pdf.disp_params["filename"], "报告.pdf")
        self.assertEqual(pdf.payload, b"%PDF-1.4\n")
        self.assertEqual(pdf.digest(),
                         hashlib.sha256(b"%PDF-1.4\n").hexdigest())

    def test_preamble_epilogue_excluded(self):
        root = parse_message(NESTED_MIXED)
        self.assertNotIn(b"Preamble", root.children[0].payload)
        self.assertNotIn(b"Epilogue", root.children[2].payload)

    def test_boundary_like_text_in_non_multipart(self):
        root = parse_message(BOUNDARY_LIKE_BODY)
        self.assertFalse(root.children)
        self.assertIn(b"--boundary\r\n", root.payload)
        self.assertIn(b"--outer-boundary--\r\n", root.payload)

    def test_boundary_prefix_does_not_split(self):
        root = parse_message(BOUNDARY_PREFIX_IN_BODY)
        self.assertEqual(len(root.children), 2)
        body = root.children[0].payload
        for marker in (b"--out", b"--outerx", b"--outer extra",
                       b" --outer", b"----outer"):
            self.assertIn(marker, body)
        self.assertEqual(root.children[1].payload, b"second part")

    def test_message_rfc822_nested(self):
        root = parse_message(MESSAGE_RFC822)
        forwarded = root.children[1]
        self.assertEqual(forwarded.content_type, "message/rfc822")
        inner = forwarded.children[0]
        self.assertEqual(inner.path, "1.2.1")
        self.assertEqual(inner.decoded_header("subject"), "转发信")
        self.assertEqual(inner.payload, b"inner body")


class TestTransferEncoding(unittest.TestCase):
    def test_quoted_printable_soft_break(self):
        root = parse_message(QP_SOFTPREAK)
        self.assertEqual(
            root.payload,
            "line onecontinues here with space\r\n中文字符\r\n".encode("utf-8"),
        )

    def test_base64_with_whitespace(self):
        root = parse_message(
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            b"aGVs\r\nbG8g\r\nd29y bGQ=\r\n")
        self.assertEqual(root.payload, b"hello world")

    def test_invalid_base64_passthrough(self):
        root = parse_message(
            b"Content-Transfer-Encoding: base64\r\n\r\n!!!\r\n")
        self.assertIn("invalid-base64-passthrough", root.defects)
        self.assertEqual(root.payload, b"!!!\r\n")

    def test_unknown_cte_passthrough(self):
        root = parse_message(
            b"Content-Transfer-Encoding: x-weird\r\n\r\nabc\r\n")
        self.assertIn("unknown-cte-passthrough:x-weird", root.defects)
        self.assertEqual(root.payload, b"abc\r\n")


class TestCharsetFallback(unittest.TestCase):
    def test_declared_charset_used(self):
        text, strategy = decode_text("中文".encode("gbk"), "gbk")
        self.assertEqual((text, strategy), ("中文", "declared:gbk"))

    def test_unknown_charset_utf8_fallback(self):
        root = parse_message(UNKNOWN_CHARSET_BODY)
        text, strategy = root.children[0].text()
        self.assertEqual(strategy, "utf-8")
        self.assertIn("中文", text)

    def test_invalid_declared_charset_latin1_fallback(self):
        root = parse_message(UNKNOWN_CHARSET_BODY)
        text, strategy = root.children[1].text()
        self.assertEqual(strategy, "latin-1-lossless")
        self.assertEqual(text, "caf\xe9 au lait")

    def test_unknown_charset_name_falls_back(self):
        text, strategy = decode_text("héllo".encode("utf-8"), "x-no-such")
        self.assertEqual((text, strategy), ("héllo", "utf-8"))


class TestDumpTree(unittest.TestCase):
    def test_dump_tree_output(self):
        out = dump_tree(parse_message(NESTED_MIXED))
        self.assertIn("1 multipart/mixed", out)
        self.assertIn("1.2.2 text/html", out)
        self.assertIn("attachment", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
