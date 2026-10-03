import unittest

from protoext.handshake import negotiate, validate_ack, validate_offer
from protoext.errors import NegotiationFailed, NegotiationContradiction


class NegotiateLogicTests(unittest.TestCase):
    def test_full_support(self):
        agreed = negotiate(["priority", "zlib"], [], ["zlib", "priority"])
        self.assertEqual(agreed, ["priority", "zlib"])

    def test_partial_support(self):
        agreed = negotiate(["priority", "zlib"], [], ["priority"])
        self.assertEqual(agreed, ["priority"])

    def test_none_supported_empty_required(self):
        agreed = negotiate(["priority", "zlib"], [], [])
        self.assertEqual(agreed, [])

    def test_required_satisfied(self):
        agreed = negotiate(["priority", "zlib"], ["zlib"], ["priority", "zlib"])
        self.assertEqual(agreed, ["priority", "zlib"])

    def test_required_missing_raises_with_capability_lists(self):
        with self.assertRaises(NegotiationFailed) as cm:
            negotiate(["priority", "zlib"], ["zlib"], ["priority"])
        err = cm.exception
        self.assertEqual(err.offered, ["priority", "zlib"])
        self.assertEqual(err.required, ["zlib"])
        self.assertEqual(err.supported, ["priority"])
        self.assertEqual(err.agreed, ["priority"])
        self.assertIn("zlib", str(err))
        self.assertIn("能力清单", str(err))

    def test_required_not_offered_is_contradiction(self):
        with self.assertRaises(NegotiationContradiction):
            negotiate(["priority"], ["zlib"], ["zlib"])

    def test_malformed_ext_field_is_contradiction(self):
        with self.assertRaises(NegotiationContradiction):
            validate_offer("zlib,priority", [])
        with self.assertRaises(NegotiationContradiction):
            validate_offer([1, 2], [])

    def test_duplicates_are_deduplicated(self):
        offered, required = validate_offer(["zlib", "zlib"], [])
        self.assertEqual(offered, ["zlib"])
        self.assertEqual(negotiate(["zlib", "zlib"], [], ["zlib"]), ["zlib"])


class AckValidationTests(unittest.TestCase):
    def test_ack_must_be_subset_of_offer(self):
        with self.assertRaises(NegotiationContradiction) as cm:
            validate_ack(["priority", "zlib"], ["priority"])
        err = cm.exception
        self.assertEqual(err.agreed, ["priority", "zlib"])
        self.assertIn("从未提供", str(err))

    def test_ack_subset_ok(self):
        self.assertEqual(validate_ack(["priority"], ["priority", "zlib"]),
                         ["priority"])

    def test_ack_wrong_type(self):
        with self.assertRaises(NegotiationContradiction):
            validate_ack("priority", ["priority"])


if __name__ == "__main__":
    unittest.main()
