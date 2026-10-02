"""DER 解码器边界与畸形编码自测。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from certlib import asn1
from certlib.errors import EncodingError, FailureCode


def expect_code(test, code, func, *args):
    with test.assertRaises(EncodingError) as ctx:
        func(*args)
    test.assertEqual(ctx.exception.code, code, str(ctx.exception))
    return ctx.exception


class TestLengthAndTag(unittest.TestCase):
    def test_short_form(self):
        node = asn1.decode_exact(b"\x04\x03abc")
        self.assertEqual(node.value, b"abc")

    def test_long_form(self):
        body = b"x" * 200
        node = asn1.decode_exact(b"\x04\x81\xc8" + body)
        self.assertEqual(node.value, body)

    def test_truncated_tag(self):
        expect_code(self, FailureCode.TRUNCATED, asn1.decode_exact, b"")

    def test_truncated_length(self):
        expect_code(self, FailureCode.TRUNCATED, asn1.decode_exact, b"\x04")

    def test_truncated_value(self):
        expect_code(self, FailureCode.TRUNCATED, asn1.decode_exact,
                    b"\x04\x05abc")

    def test_truncated_long_length_bytes(self):
        expect_code(self, FailureCode.TRUNCATED, asn1.decode_exact,
                    b"\x04\x82\x01")

    def test_indefinite_length_rejected(self):
        expect_code(self, FailureCode.INDEFINITE_LENGTH, asn1.decode_exact,
                    b"\x30\x80\x00\x00")

    def test_nonminimal_length_leading_zero(self):
        expect_code(self, FailureCode.NON_MINIMAL_LENGTH, asn1.decode_exact,
                    b"\x04\x82\x00\x80" + b"x" * 0x80)

    def test_nonminimal_length_short_value_in_long_form(self):
        expect_code(self, FailureCode.NON_MINIMAL_LENGTH, asn1.decode_exact,
                    b"\x04\x81\x7f" + b"x" * 0x7F)

    def test_length_overflow_bytes(self):
        # 9 个长度后续字节超过上限 8
        expect_code(self, FailureCode.LENGTH_OVERFLOW, asn1.decode_exact,
                    b"\x04\x89" + b"\x01" * 9)

    def test_field_too_long(self):
        # 声明 32MiB 超过 16MiB 上限（数据不必真的存在）
        expect_code(self, FailureCode.FIELD_TOO_LONG, asn1.decode_exact,
                    b"\x04\x84\x02\x00\x00\x00")

    def test_trailing_data(self):
        expect_code(self, FailureCode.TRAILING_DATA, asn1.decode_exact,
                    b"\x04\x01a\x04\x01b")

    def test_high_tag_number_rejected(self):
        expect_code(self, FailureCode.UNEXPECTED_TAG, asn1.decode_exact,
                    b"\x1f\x01\x00")

    def test_nested_constructed(self):
        inner = asn1.sequence(asn1.integer(1), asn1.integer(2))
        outer = asn1.sequence(inner, asn1.integer(3))
        node = asn1.decode_exact(outer)
        self.assertEqual(len(node.children), 2)
        self.assertEqual(len(node.children[0].children), 2)


class TestPrimitiveValues(unittest.TestCase):
    def test_integer_positive_with_sign_byte(self):
        node = asn1.decode_exact(asn1.integer(0x80))
        self.assertEqual(asn1.decode_integer(node), 0x80)

    def test_integer_nonminimal_zero(self):
        node = asn1.decode_exact(b"\x02\x02\x00\x7f")
        expect_code(self, FailureCode.NON_MINIMAL_INTEGER,
                    asn1.decode_integer, node)

    def test_integer_nonminimal_ff(self):
        node = asn1.decode_exact(b"\x02\x02\xff\x80")
        expect_code(self, FailureCode.NON_MINIMAL_INTEGER,
                    asn1.decode_integer, node)

    def test_integer_empty(self):
        node = asn1.decode_exact(b"\x02\x00")
        expect_code(self, FailureCode.TRUNCATED, asn1.decode_integer, node)

    def test_bitstring_unused_bits_set(self):
        # 声明 3 个未用位但末位非零
        node = asn1.decode_exact(b"\x03\x02\x03\xff")
        expect_code(self, FailureCode.INVALID_BITSTRING,
                    asn1.decode_bitstring, node)

    def test_bitstring_unused_out_of_range(self):
        node = asn1.decode_exact(b"\x03\x02\x08\x00")
        expect_code(self, FailureCode.INVALID_BITSTRING,
                    asn1.decode_bitstring, node)

    def test_oid_roundtrip(self):
        node = asn1.decode_exact(asn1.oid("1.2.840.113549.1.1.11"))
        self.assertEqual(asn1.decode_oid(node), "1.2.840.113549.1.1.11")

    def test_oid_truncated_component(self):
        node = asn1.decode_exact(b"\x06\x03\x2a\x03\x84")
        expect_code(self, FailureCode.INVALID_OID, asn1.decode_oid, node)

    def test_utf8_string(self):
        node = asn1.decode_exact(asn1.utf8_string("证书"))
        self.assertEqual(asn1.decode_string(node), "证书")

    def test_invalid_utf8(self):
        node = asn1.decode_exact(b"\x0c\x02\xff\xfe")
        expect_code(self, FailureCode.INVALID_UTF8, asn1.decode_string, node)


if __name__ == "__main__":
    unittest.main()
