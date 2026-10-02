"""X.509 解析自测：字段提取与畸形/缺失字段。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from certlib import asn1, parse_certificate
from certlib.errors import CertError, FailureCode
from certlib.x509 import parse_time

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load(name):
    return (FIXTURES / f"{name}.der").read_bytes()


def expect_code(test, code, func, *args):
    with test.assertRaises(CertError) as ctx:
        func(*args)
    test.assertEqual(ctx.exception.code, code, str(ctx.exception))


class TestFieldExtraction(unittest.TestCase):
    def test_leaf_server_fields(self):
        cert = parse_certificate(load("leaf_server"))
        self.assertEqual(cert.subject.to_dict()["CN"], "server.example.com")
        self.assertEqual(cert.issuer.to_dict()["CN"], "Test Intermediate CA")
        self.assertEqual(cert.serial_number, 3)
        self.assertLess(cert.not_before, cert.not_after)
        self.assertEqual(
            cert.public_key.algorithm_oid, "1.2.840.113549.1.1.1"
        )
        self.assertEqual(cert.public_key.exponent, 65537)
        self.assertGreaterEqual(cert.public_key.modulus.bit_length(), 511)
        self.assertEqual(
            cert.extensions.key_usage,
            frozenset({"digitalSignature", "keyEncipherment"}),
        )
        self.assertEqual(
            cert.extensions.extended_key_usage,
            frozenset({"1.3.6.1.5.5.7.3.1"}),
        )
        self.assertFalse(cert.extensions.basic_constraints_ca)

    def test_root_ca_fields(self):
        cert = parse_certificate(load("root_ca"))
        self.assertEqual(cert.subject, cert.issuer)
        self.assertTrue(cert.extensions.basic_constraints_ca)
        self.assertEqual(cert.extensions.path_len, 1)
        self.assertIn("keyCertSign", cert.extensions.key_usage)

    def test_intermediate_path_len_zero(self):
        cert = parse_certificate(load("intermediate_ca"))
        self.assertEqual(cert.extensions.path_len, 0)


class TestMalformedFixtures(unittest.TestCase):
    def test_truncated(self):
        expect_code(self, FailureCode.TRUNCATED,
                    parse_certificate, load("malformed_truncated"))

    def test_indefinite_length(self):
        expect_code(self, FailureCode.INDEFINITE_LENGTH,
                    parse_certificate, load("malformed_indefinite_length"))

    def test_huge_length(self):
        with self.assertRaises(CertError) as ctx:
            parse_certificate(load("malformed_huge_length"))
        self.assertIn(ctx.exception.code,
                      (FailureCode.FIELD_TOO_LONG, FailureCode.TRUNCATED))

    def test_trailing(self):
        expect_code(self, FailureCode.TRAILING_DATA,
                    parse_certificate, load("malformed_trailing"))

    def test_nonminimal_integer(self):
        expect_code(self, FailureCode.NON_MINIMAL_INTEGER,
                    parse_certificate, load("malformed_nonminimal_integer"))

    def test_missing_required_fields(self):
        expect_code(self, FailureCode.MISSING_FIELD,
                    parse_certificate, load("malformed_missing_fields"))

    def test_empty_input(self):
        expect_code(self, FailureCode.TRUNCATED, parse_certificate, b"")


if __name__ == "__main__":
    unittest.main()


class TestTimeForms(unittest.TestCase):
    def test_utc_time(self):
        node = asn1.decode_exact(b"\x17\x0d491231235959Z")
        self.assertEqual(parse_time(node).year, 2049)

    def test_generalized_time(self):
        node = asn1.decode_exact(b"\x18\x0f20500101000000Z")
        self.assertEqual(parse_time(node).year, 2050)

    def test_bad_time(self):
        node = asn1.decode_exact(b"\x17\x0d999999999999Z")
        expect_code(self, FailureCode.INVALID_TIME, parse_time, node)
