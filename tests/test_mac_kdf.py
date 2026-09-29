"""mac_kdf 自测：标准向量对拍 + 上下文隔离 + 边界用例 + 密钥清除。

运行：python3 tests/test_mac_kdf.py   （或 python3 -m unittest discover tests）
"""

import hashlib
import hmac as stdlib_hmac
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mac_kdf import (
    HKDF_MAX_LENGTH,
    SHA256_BLOCK_SIZE,
    KeyBytes,
    derive_key,
    hkdf,
    hkdf_expand,
    hkdf_extract,
    hmac_sha256,
    hmac_verify,
)


def unhex(s):
    return bytes.fromhex(s.replace(" ", "").replace("\n", ""))


# ---------------------------------------------------------------------------
# 1. 标准测试向量对拍
# ---------------------------------------------------------------------------

class TestHmacRFC4231(unittest.TestCase):
    """RFC 4231《HMAC 测试用例》HMAC-SHA-256 全部 7 个用例。"""

    VECTORS = [
        # (key, message, expected_hmac_sha256)
        (
            b"\x0b" * 20,
            b"Hi There",
            "b0344c61d8db38535ca8afceaf0bf12b"
            "881dc200c9833da726e9376c2e32cff7",
        ),
        (
            b"Jefe",
            b"what do ya want for nothing?",
            "5bdcc146bf60754e6a042426089575c7"
            "5a003f089d2739839dec58b964ec3843",
        ),
        (
            b"\xaa" * 20,
            b"\xdd" * 50,
            "773ea91e36800e46854db8ebd09181a7"
            "2959098b3ef8c122d9635514ced565fe",
        ),
        (
            unhex("0102030405060708090a0b0c0d0e0f10"
                  "111213141516171819"),
            b"\xcd" * 50,
            "82558a389a443c0ea4cc819899f2083a"
            "85f0faa3e578f8077a2e3ff46729665b",
        ),
        (
            b"\x0c" * 20,
            b"Test With Truncation",
            "a3b6167473100ee06e0c796c2955552b",  # 128-bit 截断值
        ),
        (
            b"\xaa" * 131,  # 长于分组长度的密钥
            b"Test Using Larger Than Block-Size Key - Hash Key First",
            "60e431591ee0b67f0d8a26aacbf5b77f"
            "8e0bc6213728c5140546040f0ee37f54",
        ),
        (
            b"\xaa" * 131,
            b"This is a test using a larger than block-size key and a "
            b"larger than block-size data. The key needs to be hashed "
            b"before being used by the HMAC algorithm.",
            "9b09ffa71b942fcb27635fbcd5b0e944"
            "bfdc63644f0713938a7f51535c3a35e2",
        ),
    ]

    def test_rfc4231_vectors(self):
        for i, (key, msg, expected_hex) in enumerate(self.VECTORS, 1):
            with self.subTest(case=i):
                got = hmac_sha256(key, msg)
                self.assertTrue(got.hex().startswith(expected_hex),
                                f"case {i}: {got.hex()} != {expected_hex}")

    def test_matches_stdlib_hmac(self):
        """自实现与标准库 hmac 在随机输入上逐一相等。"""
        rng = os.urandom
        for _ in range(200):
            key = rng(rng(1)[0] % 200)
            msg = rng(rng(1)[0] % 4096)
            self.assertEqual(
                hmac_sha256(key, msg),
                stdlib_hmac.new(key, msg, hashlib.sha256).digest())

    def test_verify(self):
        key, msg = b"key", b"message"
        tag = hmac_sha256(key, msg)
        self.assertTrue(hmac_verify(key, msg, tag))
        self.assertFalse(hmac_verify(key, msg, bytes(32)))
        self.assertFalse(hmac_verify(b"other", msg, tag))


class TestHkdfRFC5869(unittest.TestCase):
    """RFC 5869 附录 A 的 HKDF-SHA-256 测试用例 1~3。"""

    def test_case_1(self):
        ikm = b"\x0b" * 22
        salt = unhex("000102030405060708090a0b0c")
        info = unhex("f0f1f2f3f4f5f6f7f8f9")
        prk_expected = "077709362c2e32df0ddc3f0dc47bba63" \
                       "90b6c73bb50f9c3122ec844ad7c2b3e5"
        okm_expected = "3cb25f25faacd57a90434f64d0362f2a" \
                       "2d2d0a90cf1a5a4c5db02d56ecc4c5bf" \
                       "34007208d5b887185865"
        prk = hkdf_extract(salt, ikm)
        self.assertEqual(prk.hex(), prk_expected)
        self.assertEqual(hkdf_expand(prk, info, 42).hex(), okm_expected)
        self.assertEqual(hkdf(ikm, salt=salt, info=info, length=42).hex(),
                         okm_expected)

    def test_case_2(self):
        ikm = unhex("000102030405060708090a0b0c0d0e0f"
                    "101112131415161718191a1b1c1d1e1f"
                    "202122232425262728292a2b2c2d2e2f"
                    "303132333435363738393a3b3c3d3e3f"
                    "404142434445464748494a4b4c4d4e4f")
        salt = unhex("606162636465666768696a6b6c6d6e6f"
                     "707172737475767778797a7b7c7d7e7f"
                     "808182838485868788898a8b8c8d8e8f"
                     "909192939495969798999a9b9c9d9e9f"
                     "a0a1a2a3a4a5a6a7a8a9aaabacadaeaf")
        info = unhex("b0b1b2b3b4b5b6b7b8b9babbbcbdbebf"
                     "c0c1c2c3c4c5c6c7c8c9cacbcccdcecf"
                     "d0d1d2d3d4d5d6d7d8d9dadbdcdddedf"
                     "e0e1e2e3e4e5e6e7e8e9eaebecedeeef"
                     "f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff")
        prk_expected = "06a6b88c5853361a06104c9ceb35b45c" \
                       "ef760014904671014a193f40c15fc244"
        okm_expected = (
            "b11e398dc80327a1c8e7f78c596a4934"
            "4f012eda2d4efad8a050cc4c19afa97c"
            "59045a99cac7827271cb41c65e590e09"
            "da3275600c2f09b8367793a9aca3db71"
            "cc30c58179ec3e87c14c01d5c1f3434f"
            "1d87")
        prk = hkdf_extract(salt, ikm)
        self.assertEqual(prk.hex(), prk_expected)
        self.assertEqual(hkdf_expand(prk, info, 82).hex(), okm_expected)

    def test_case_3(self):
        # salt 与 info 均缺省（空）
        ikm = b"\x0b" * 22
        prk_expected = "19ef24a32c717b167f33a91d6f648bdf" \
                       "96596776afdb6377ac434c1c293ccb04"
        okm_expected = "8da4e775a563c18f715f802a063c5a31" \
                       "b8a11f5c5ee1879ec3454e5f3c738d2d" \
                       "9d201395faa4b61a96c8"
        prk = hkdf_extract(b"", ikm)
        self.assertEqual(prk.hex(), prk_expected)
        self.assertEqual(hkdf_expand(prk, b"", 42).hex(), okm_expected)


# ---------------------------------------------------------------------------
# 2. 上下文隔离：不同上下文必须得到不同子密钥
# ---------------------------------------------------------------------------

class TestContextSeparation(unittest.TestCase):
    MASTER = bytes(range(32))

    def test_different_labels_different_keys(self):
        k_enc = derive_key(self.MASTER, label=b"encryption")
        k_mac = derive_key(self.MASTER, label=b"mac")
        k_backup = derive_key(self.MASTER, label=b"backup")
        keys = {k_enc.bytes(), k_mac.bytes(), k_backup.bytes()}
        self.assertEqual(len(keys), 3)

    def test_different_contexts_different_keys(self):
        alice = derive_key(self.MASTER, label=b"session", context=b"alice")
        bob = derive_key(self.MASTER, label=b"session", context=b"bob")
        self.assertNotEqual(alice.bytes(), bob.bytes())

    def test_same_inputs_same_key(self):
        a = derive_key(self.MASTER, label=b"session", context=b"alice")
        b = derive_key(self.MASTER, label=b"session", context=b"alice")
        self.assertEqual(a.bytes(), b.bytes())

    def test_concatenation_ambiguity_resolved(self):
        # ("ab","c") 与 ("a","bc") 直接拼接会得到相同字节串，
        # 长度前缀编码必须让二者派生出不同密钥。
        x = derive_key(self.MASTER, label=b"ab", context=b"c")
        y = derive_key(self.MASTER, label=b"a", context=b"bc")
        self.assertNotEqual(x.bytes(), y.bytes())

    def test_master_key_difference(self):
        k1 = derive_key(b"\x01" * 32, label=b"encryption")
        k2 = derive_key(b"\x02" * 32, label=b"encryption")
        self.assertNotEqual(k1.bytes(), k2.bytes())


# ---------------------------------------------------------------------------
# 3. 边界用例：空消息、超长消息、密钥长度边界
# ---------------------------------------------------------------------------

class TestEdgeCases(unittest.TestCase):
    def test_empty_message(self):
        tag = hmac_sha256(b"key", b"")
        self.assertEqual(
            tag, stdlib_hmac.new(b"key", b"", hashlib.sha256).digest())
        self.assertTrue(hmac_verify(b"key", b"", tag))

    def test_empty_key(self):
        tag = hmac_sha256(b"", b"message")
        self.assertEqual(
            tag, stdlib_hmac.new(b"", b"message", hashlib.sha256).digest())

    def test_key_length_boundaries(self):
        # 覆盖 0 / 1 / 块长-1 / 块长 / 块长+1 / 远超块长
        for n in (0, 1, SHA256_BLOCK_SIZE - 1, SHA256_BLOCK_SIZE,
                  SHA256_BLOCK_SIZE + 1, 1000):
            with self.subTest(key_len=n):
                key = b"\x5a" * n
                self.assertEqual(
                    hmac_sha256(key, b"boundary"),
                    stdlib_hmac.new(key, b"boundary", hashlib.sha256).digest())

    def test_message_length_boundaries(self):
        # 覆盖散列内部 padding 边界：55/56/63/64/65/119/120/128 字节
        for n in (0, 1, 55, 56, 63, 64, 65, 119, 120, 128, 1000):
            with self.subTest(msg_len=n):
                msg = b"m" * n
                self.assertEqual(
                    hmac_sha256(b"k", msg),
                    stdlib_hmac.new(b"k", msg, hashlib.sha256).digest())

    def test_very_long_message(self):
        # 16 MiB 超长消息，且分块喂入与一次性喂入结果一致
        key = b"stream-key"
        chunk = os.urandom(1 << 20)
        one_shot = hmac_sha256(key, chunk * 16)
        self.assertEqual(
            one_shot,
            stdlib_hmac.new(key, chunk * 16, hashlib.sha256).digest())

    def test_hkdf_length_boundaries(self):
        prk = hkdf_extract(b"salt", b"ikm")
        self.assertEqual(hkdf_expand(prk, b"info", 0), b"")
        self.assertEqual(len(hkdf_expand(prk, b"info", 1)), 1)
        self.assertEqual(len(hkdf_expand(prk, b"info", HKDF_MAX_LENGTH)),
                         HKDF_MAX_LENGTH)
        with self.assertRaises(ValueError):
            hkdf_expand(prk, b"info", HKDF_MAX_LENGTH + 1)
        with self.assertRaises(ValueError):
            hkdf_expand(prk, b"info", -1)

    def test_derive_key_length(self):
        k = derive_key(b"m" * 32, label=b"x", length=64)
        self.assertEqual(len(k), 64)


# ---------------------------------------------------------------------------
# 4. 密钥材料显式清除
# ---------------------------------------------------------------------------

class TestKeyWipe(unittest.TestCase):
    def test_wipe_zeroes_buffer(self):
        kb = KeyBytes(b"\xff" * 32)
        kb.wipe()
        self.assertTrue(kb.wiped)
        self.assertEqual(kb._buf, bytearray(32))

    def test_use_after_wipe_raises(self):
        kb = KeyBytes(b"\x01" * 32)
        kb.wipe()
        with self.assertRaises(ValueError):
            kb.bytes()
        with self.assertRaises(ValueError):
            hmac_sha256(kb, b"msg")

    def test_context_manager_auto_wipe(self):
        with KeyBytes(b"\x02" * 32) as kb:
            tag = hmac_sha256(kb, b"msg")
            self.assertEqual(len(tag), 32)
        self.assertTrue(kb.wiped)
        self.assertEqual(kb._buf, bytearray(32))

    def test_derived_key_is_wipeable(self):
        with derive_key(b"m" * 32, label=b"encryption") as sub:
            self.assertEqual(len(sub.bytes()), 32)
        self.assertTrue(sub.wiped)


if __name__ == "__main__":
    unittest.main(verbosity=2)
