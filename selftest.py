#!/usr/bin/env python3
"""Self-tests and standard-vector checks for crypto_keys.py."""

from __future__ import annotations

import hashlib
import hmac as stdlib_hmac
import unittest

from crypto_keys import (
    HMAC,
    InvalidMACError,
    KeyDeriver,
    KeyMaterialError,
    SecretBytes,
    hkdf_expand,
    hkdf_extract,
    hmac_digest,
    verify_mac,
    verify_mac_or_raise,
    zeroize,
)


def stdlib_tag(key: bytes, message: bytes) -> bytes:
    return stdlib_hmac.new(key, message, hashlib.sha256).digest()


class HMACStandardVectorTests(unittest.TestCase):
    def test_rfc_4231_sha256_vectors(self) -> None:
        vectors = [
            (
                bytes.fromhex("0b" * 20),
                b"Hi There",
                "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7",
            ),
            (
                b"Jefe",
                b"what do ya want for nothing?",
                "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843",
            ),
            (
                bytes.fromhex("aa" * 20),
                bytes.fromhex("dd" * 50),
                "773ea91e36800e46854db8ebd09181a72959098b3ef8c122d9635514ced565fe",
            ),
            (
                bytes(range(1, 26)),
                bytes.fromhex("cd" * 50),
                "82558a389a443c0ea4cc819899f2083a85f0faa3e578f8077a2e3ff46729665b",
            ),
            (
                bytes.fromhex("aa" * 131),
                b"Test Using Larger Than Block-Size Key - Hash Key First",
                "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54",
            ),
            (
                bytes.fromhex("aa" * 131),
                b"This is a test using a larger than block-size key and a larger "
                b"than block-size data. The key needs to be hashed before being "
                b"used by the HMAC algorithm.",
                "9b09ffa71b942fcb27635fbcd5b0e944bfdc63644f0713938a7f51535c3a35e2",
            ),
        ]

        for key, message, expected in vectors:
            with self.subTest(key_length=len(key), message_length=len(message)):
                self.assertEqual(hmac_digest(key, message).hex(), expected)


class HKDFStandardVectorTests(unittest.TestCase):
    def test_rfc_5869_test_case_1(self) -> None:
        ikm = bytes.fromhex("0b" * 22)
        salt = bytes.fromhex("000102030405060708090a0b0c")
        info = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
        prk = hkdf_extract(salt, ikm)
        okm = hkdf_expand(prk, info, 42)
        self.assertEqual(
            prk.hex(),
            "077709362c2e32df0ddc3f0dc47bba6390b6c73bb50f9c3122ec844ad7c2b3e5",
        )
        self.assertEqual(
            okm.hex(),
            "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
            "34007208d5b887185865",
        )

    def test_rfc_5869_test_case_3_empty_salt_and_info(self) -> None:
        ikm = bytes.fromhex("0b" * 22)
        prk = hkdf_extract(b"", ikm)
        okm = hkdf_expand(prk, b"", 42)
        self.assertEqual(
            prk.hex(),
            "19ef24a32c717b167f33a91d6f648bdf96596776afdb6377ac434c1c293ccb04",
        )
        self.assertEqual(
            okm.hex(),
            "8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d"
            "9d201395faa4b61a96c8",
        )


class BoundaryTests(unittest.TestCase):
    def test_empty_key_and_empty_message(self) -> None:
        self.assertEqual(
            hmac_digest(b"", b"").hex(),
            "b613679a0814d9ec772f95d778c35fc5ff1697c493715653c6c712144292c5ad",
        )

    def test_key_lengths_around_sha256_block_boundary(self) -> None:
        for length in (0, 1, 31, 32, 63, 64, 65, 127, 128, 129, 131):
            key = bytes((index % 251 for index in range(length)))
            with self.subTest(length=length):
                self.assertEqual(hmac_digest(key, b""), stdlib_tag(key, b""))

    def test_one_mib_message(self) -> None:
        key = b"boundary-key"
        message = b"abcd" * 262_144
        self.assertEqual(len(message), 1_048_576)
        self.assertEqual(hmac_digest(key, message), stdlib_tag(key, message))

    def test_incremental_hmac_matches_single_shot(self) -> None:
        incremental = HMAC(b"incremental-key")
        for chunk in (b"", b"chunk-0", b"chunk-1", b"x" * 1000):
            incremental.update(chunk)
        message = b"chunk-0chunk-1" + b"x" * 1000
        self.assertEqual(incremental.digest(), hmac_digest(b"incremental-key", message))

    def test_sha384_and_sha512_match_standard_library(self) -> None:
        key = b"other-hash-key"
        message = b"same authenticated message"
        for algorithm in ("sha384", "sha512"):
            with self.subTest(algorithm=algorithm):
                expected = stdlib_hmac.new(
                    key, message, getattr(hashlib, algorithm)
                ).digest()
                self.assertEqual(hmac_digest(key, message, algorithm), expected)

    def test_secret_bytes_can_be_used_as_key_material(self) -> None:
        wrapped_key = SecretBytes(b"wrapped-key")
        try:
            self.assertEqual(
                hmac_digest(wrapped_key, b"message"),
                stdlib_tag(b"wrapped-key", b"message"),
            )
        finally:
            wrapped_key.clear()

    def test_hkdf_output_length_boundaries(self) -> None:
        prk = hkdf_extract(b"salt", b"input-key-material")
        for length in (1, 31, 32, 33, 8159, 8160):
            with self.subTest(length=length):
                self.assertEqual(len(hkdf_expand(prk, b"info", length)), length)
        with self.assertRaises(ValueError):
            hkdf_expand(prk, b"info", 8161)
        with self.assertRaises(ValueError):
            hkdf_expand(prk, b"info", 0)

    def test_short_prk_rejected(self) -> None:
        with self.assertRaises(KeyMaterialError):
            hkdf_expand(b"x" * 31, b"info", 32)

    def test_one_byte_and_oversized_master_key(self) -> None:
        for master_key in (b"k", bytes.fromhex("ab" * 1000)):
            with KeyDeriver(master_key) as deriver:
                self.assertEqual(
                    len(deriver.derive(64, context=b"api").data),
                    64,
                )


class IntegrityAndIsolationTests(unittest.TestCase):
    def test_tag_verification_accepts_and_rejects_changes(self) -> None:
        key = b"integrity-key"
        message = b"transfer $10"
        tag = hmac_digest(key, message)
        self.assertTrue(verify_mac(key, message, tag))
        self.assertFalse(verify_mac(key, b"transfer $999", tag))
        self.assertFalse(verify_mac(b"other-key", message, tag))
        self.assertFalse(verify_mac(key, message, b"\x00" * len(tag)))
        verify_mac_or_raise(key, message, tag)
        with self.assertRaises(InvalidMACError):
            verify_mac_or_raise(key, message + b"!", tag)

    def test_appended_extension_is_not_accepted_from_original_tag(self) -> None:
        key = b"length-extension-guarded"
        message = b"original authenticated message"
        tag = hmac_digest(key, message)
        self.assertFalse(verify_mac(key, message + b"\x80extra", tag))
        self.assertNotEqual(tag, hmac_digest(key, message + b"\x80extra"))

    def test_different_contexts_derive_different_keys(self) -> None:
        with KeyDeriver(b"master-key") as deriver:
            encryption_key = deriver.derive(32, context=b"encryption/v1")
            signing_key = deriver.derive(32, context=b"message-auth/v1")
            other_encryption_key = deriver.derive(32, context=b"encryption/v2")

        self.assertNotEqual(encryption_key, signing_key)
        self.assertNotEqual(encryption_key, other_encryption_key)

    def test_different_labels_and_infos_derive_different_keys(self) -> None:
        with KeyDeriver(b"master-key") as deriver:
            request_key = deriver.derive(32, context=b"api/v1", label=b"request")
            response_key = deriver.derive(32, context=b"api/v1", label=b"response")
            alice_key = deriver.derive(32, context=b"api/v1", info=b"alice")
            bob_key = deriver.derive(32, context=b"api/v1", info=b"bob")

        self.assertNotEqual(request_key, response_key)
        self.assertNotEqual(alice_key, bob_key)

    def test_length_prefixed_context_fields_do_not_collide(self) -> None:
        with KeyDeriver(b"master-key") as deriver:
            first = deriver.derive(32, context=b"a/b", label=b"c")
            second = deriver.derive(32, context=b"a", label=b"b/c")
        self.assertNotEqual(first, second)

    def test_output_length_is_bound_to_derivation_domain(self) -> None:
        with KeyDeriver(b"master-key") as deriver:
            short = deriver.derive(32, context=b"same-context")
            longer = deriver.derive(64, context=b"same-context")
        self.assertNotEqual(short.reveal(), longer.reveal()[:32])

    def test_empty_context_rejected(self) -> None:
        with KeyDeriver(b"master-key") as deriver:
            with self.assertRaises(KeyMaterialError):
                deriver.derive(32, context=b"")

    def test_empty_master_key_rejected(self) -> None:
        with self.assertRaises(KeyMaterialError):
            KeyDeriver(b"")


class ClearingTests(unittest.TestCase):
    def test_secret_bytes_can_be_explicitly_cleared(self) -> None:
        secret = SecretBytes(b"sensitive-derived-key")
        self.assertEqual(secret.reveal(), b"sensitive-derived-key")
        self.assertFalse(secret.cleared)
        secret.clear()
        self.assertTrue(secret.cleared)
        self.assertEqual(
            bytes(secret._data), b"\x00" * len(b"sensitive-derived-key")
        )
        with self.assertRaises(ValueError):
            secret.reveal()

    def test_context_manager_clears_secret(self) -> None:
        with SecretBytes(b"temporary") as secret:
            self.assertEqual(len(secret), 9)
        self.assertTrue(secret.cleared)

    def test_caller_key_and_deriver_can_be_cleared(self) -> None:
        key = bytearray(b"master-key-material")
        deriver = KeyDeriver(key)
        derived = deriver.derive(32, context=b"test")
        zeroize(key)
        self.assertEqual(key, bytearray(len(key)))
        deriver.clear()
        self.assertTrue(deriver.cleared)
        with self.assertRaises(ValueError):
            deriver.derive(32, context=b"test")
        self.assertEqual(len(derived), 32)
        derived.clear()
        self.assertTrue(derived.cleared)

    def test_hmac_can_be_cleared(self) -> None:
        hmac_state = HMAC(b"key")
        self.assertFalse(hmac_state.cleared)
        hmac_state.update(b"message")
        hmac_state.clear()
        self.assertTrue(hmac_state.cleared)
        with self.assertRaises(ValueError):
            hmac_state.update(b"more")
        with self.assertRaises(ValueError):
            hmac_state.digest()


if __name__ == "__main__":
    unittest.main(verbosity=2)
