"""校验自测：有效期、用途、链式签发关系的失败分类。"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from certlib import (
    FailureCode,
    build_chain,
    check_usage,
    check_validity,
    parse_certificate,
)
from certlib.errors import ValidationError

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def cert(name):
    return parse_certificate((FIXTURES / f"{name}.der").read_bytes())


NOW = datetime.now(timezone.utc)


class TestValidity(unittest.TestCase):
    def test_valid_cert_passes(self):
        check_validity(cert("leaf_server"))

    def test_expired(self):
        with self.assertRaises(ValidationError) as ctx:
            check_validity(cert("leaf_expired"))
        self.assertEqual(ctx.exception.code, FailureCode.EXPIRED)

    def test_not_yet_valid(self):
        with self.assertRaises(ValidationError) as ctx:
            check_validity(cert("leaf_not_yet_valid"))
        self.assertEqual(ctx.exception.code, FailureCode.NOT_YET_VALID)

    def test_boundary_not_before(self):
        c = cert("leaf_server")
        check_validity(c, now=c.not_before)  # 恰好在 notBefore 视为有效

    def test_boundary_not_after(self):
        c = cert("leaf_server")
        with self.assertRaises(ValidationError) as ctx:
            check_validity(c, now=c.not_after + timedelta(seconds=1))
        self.assertEqual(ctx.exception.code, FailureCode.EXPIRED)


class TestUsage(unittest.TestCase):
    def test_server_usage_ok(self):
        check_usage(cert("leaf_server"),
                    required_key_usages={"digitalSignature"},
                    required_ekus={"1.3.6.1.5.5.7.3.1"})

    def test_client_cert_as_server_fails(self):
        with self.assertRaises(ValidationError) as ctx:
            check_usage(cert("leaf_client"),
                        required_ekus={"1.3.6.1.5.5.7.3.1"})
        self.assertEqual(ctx.exception.code, FailureCode.USAGE_MISMATCH)

    def test_missing_key_usage_bit(self):
        with self.assertRaises(ValidationError) as ctx:
            check_usage(cert("leaf_client"),
                        required_key_usages={"keyEncipherment"})
        self.assertEqual(ctx.exception.code, FailureCode.USAGE_MISMATCH)


class TestChain(unittest.TestCase):
    def test_full_chain_ok(self):
        report = build_chain(
            [cert("leaf_server"), cert("intermediate_ca"), cert("root_ca")],
            required_ekus={"1.3.6.1.5.5.7.3.1"},
        )
        self.assertTrue(report.ok, [str(i) for i in report.issues])

    def test_expired_in_chain(self):
        report = build_chain(
            [cert("leaf_expired"), cert("intermediate_ca"), cert("root_ca")]
        )
        self.assertIn(FailureCode.EXPIRED, report.codes())

    def test_issuer_mismatch(self):
        report = build_chain([cert("leaf_server"), cert("rogue_root")])
        self.assertIn(FailureCode.ISSUER_MISMATCH, report.codes())

    def test_signature_invalid(self):
        report = build_chain(
            [cert("leaf_bad_signature"), cert("intermediate_ca"),
             cert("root_ca")]
        )
        self.assertIn(FailureCode.SIGNATURE_INVALID, report.codes())

    def test_not_a_ca(self):
        report = build_chain(
            [cert("leaf_issued_by_leaf"), cert("leaf_server"),
             cert("intermediate_ca"), cert("root_ca")]
        )
        self.assertIn(FailureCode.NOT_A_CA, report.codes())

    def test_path_len_exceeded(self):
        # root pathLen=1，中间 CA pathLen=0；在中间 CA 下再挂一级“中间”
        # 即 leaf_issued_by_leaf 链深 4 级时中间 CA 下方有 1 个中间 CA，
        # 超过其 pathLen=0。
        report = build_chain(
            [cert("leaf_issued_by_leaf"), cert("leaf_server"),
             cert("intermediate_ca"), cert("root_ca")]
        )
        self.assertIn(FailureCode.PATH_LENGTH_EXCEEDED, report.codes())

    def test_usage_mismatch_in_chain(self):
        report = build_chain(
            [cert("leaf_client"), cert("intermediate_ca"), cert("root_ca")],
            required_ekus={"1.3.6.1.5.5.7.3.1"},
        )
        self.assertIn(FailureCode.USAGE_MISMATCH, report.codes())

    def test_empty_chain(self):
        report = build_chain([])
        self.assertIn(FailureCode.MISSING_FIELD, report.codes())


if __name__ == "__main__":
    unittest.main()
