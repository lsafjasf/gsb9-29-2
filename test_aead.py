"""aead 库的自测：功能、篡改全拒、随机化与边界用例。

运行方式：python3 test_aead.py -v
"""

import struct
import unittest
from unittest import mock

import aead

KEY = aead.generate_key()
AAD = b"header:tenant=uid53;ctx=demo"
PLAINTEXT = b"hello, authenticated encryption!"


def flip_bit(data: bytes, offset: int) -> bytes:
    buf = bytearray(data)
    buf[offset] ^= 0x01
    return bytes(buf)


class RoundTripTest(unittest.TestCase):
    def test_basic_roundtrip(self):
        packet = aead.seal(KEY, PLAINTEXT, AAD)
        plaintext, aad = aead.open(KEY, packet)
        self.assertEqual(plaintext, PLAINTEXT)
        self.assertEqual(aad, AAD)

    def test_roundtrip_without_aad(self):
        packet = aead.seal(KEY, PLAINTEXT)
        plaintext, aad = aead.open(KEY, packet)
        self.assertEqual(plaintext, PLAINTEXT)
        self.assertEqual(aad, b"")

    def test_wrong_key_rejected(self):
        packet = aead.seal(KEY, PLAINTEXT, AAD)
        with self.assertRaises(aead.TagMismatchError):
            aead.open(aead.generate_key(), packet)


class RandomizationTest(unittest.TestCase):
    def test_same_plaintext_seals_differently(self):
        packet1 = aead.seal(KEY, PLAINTEXT, AAD)
        packet2 = aead.seal(KEY, PLAINTEXT, AAD)
        self.assertNotEqual(packet1, packet2)
        self.assertEqual(aead.open(KEY, packet1), aead.open(KEY, packet2))
        self.assertEqual(aead.open(KEY, packet1)[0], PLAINTEXT)


class TamperTest(unittest.TestCase):
    """篡改必须全拒，且每类篡改对应明确的错误类型。"""

    def setUp(self):
        self.packet = aead.seal(KEY, PLAINTEXT, AAD)
        self.aad_len = len(AAD)
        self.ct_offset = 1 + 4 + self.aad_len + 8 + aead.NONCE_SIZE

    def test_ciphertext_bitflip(self):
        tampered = flip_bit(self.packet, self.ct_offset + 3)
        with self.assertRaises(aead.TagMismatchError):
            aead.open(KEY, tampered)

    def test_header_aad_bitflip(self):
        tampered = flip_bit(self.packet, 1 + 4 + 1)  # AAD 区域内
        with self.assertRaises(aead.TagMismatchError):
            aead.open(KEY, tampered)

    def test_nonce_bitflip(self):
        tampered = flip_bit(self.packet, self.ct_offset - 1)
        with self.assertRaises(aead.TagMismatchError):
            aead.open(KEY, tampered)

    def test_tag_bitflip(self):
        tampered = flip_bit(self.packet, len(self.packet) - 1)
        with self.assertRaises(aead.TagMismatchError):
            aead.open(KEY, tampered)

    def test_version_byte_tamper(self):
        tampered = flip_bit(self.packet, 0)
        with self.assertRaises(aead.MalformedPacketError):
            aead.open(KEY, tampered)

    def test_field_reorder_aad_and_ciphertext(self):
        """调换 AAD 与密文字段：长度解析自相矛盾或标签失效。"""
        aad_start = 1 + 4
        aad_end = aad_start + self.aad_len
        ct_end = len(self.packet) - aead.TAG_SIZE
        reordered = (
            self.packet[:aad_start]
            + self.packet[aad_end:ct_end]  # nonce+密文被挪到 AAD 位置
            + self.packet[aad_start:aad_end]
            + self.packet[-aead.TAG_SIZE:]
        )
        with self.assertRaises(aead.AEADError) as ctx:
            aead.open(KEY, reordered)
        self.assertIsInstance(
            ctx.exception,
            (
                aead.MalformedPacketError,
                aead.TruncatedDataError,
                aead.TagMismatchError,
            ),
        )

    def test_field_reorder_length_fields(self):
        """调换两个长度字段：结构校验直接拒绝。"""
        ct_len_offset = 1 + 4 + self.aad_len
        tampered = (
            self.packet[:1]
            + self.packet[ct_len_offset : ct_len_offset + 4]  # CT_LEN 高 4 字节
            + self.packet[5:ct_len_offset]
            + self.packet[1:5]  # AAD_LEN 被挪到 CT_LEN 位置
            + self.packet[ct_len_offset + 8 :]
        )
        with self.assertRaises(aead.AEADError) as ctx:
            aead.open(KEY, tampered)
        self.assertIsInstance(
            ctx.exception,
            (
                aead.MalformedPacketError,
                aead.TruncatedDataError,
                aead.TagMismatchError,
            ),
        )

    def test_ciphertext_block_reorder(self):
        """密文内部两块对调：长度不变，必须靠标签检出。"""
        half = self.ct_offset + len(PLAINTEXT) // 2
        tampered = (
            self.packet[: self.ct_offset]
            + self.packet[half : self.ct_offset + len(PLAINTEXT)]
            + self.packet[self.ct_offset : half]
            + self.packet[self.ct_offset + len(PLAINTEXT) :]
        )
        with self.assertRaises(aead.TagMismatchError):
            aead.open(KEY, tampered)

    def test_truncation(self):
        for cut in (1, aead.TAG_SIZE, len(self.packet) // 2):
            with self.subTest(cut=cut):
                with self.assertRaises(aead.TruncatedDataError):
                    aead.open(KEY, self.packet[: len(self.packet) - cut])

    def test_truncation_to_empty(self):
        with self.assertRaises(aead.TruncatedDataError):
            aead.open(KEY, b"")

    def test_trailing_garbage(self):
        tampered = self.packet + b"\x00"
        with self.assertRaises(aead.MalformedPacketError):
            aead.open(KEY, tampered)


class EdgeCaseTest(unittest.TestCase):
    def test_empty_plaintext(self):
        packet = aead.seal(KEY, b"", AAD)
        plaintext, aad = aead.open(KEY, packet)
        self.assertEqual(plaintext, b"")
        self.assertEqual(aad, AAD)

    def test_empty_plaintext_tamper_still_detected(self):
        packet = aead.seal(KEY, b"", AAD)
        tampered = flip_bit(packet, len(packet) - 1)
        with self.assertRaises(aead.TagMismatchError):
            aead.open(KEY, tampered)

    def test_large_plaintext(self):
        big = bytes(range(256)) * (4 * 1024 * 1024 // 256)  # 4 MiB
        packet = aead.seal(KEY, big, AAD)
        plaintext, _ = aead.open(KEY, packet)
        self.assertEqual(plaintext, big)

    def test_plaintext_size_limit(self):
        with mock.patch.object(aead, "MAX_PLAINTEXT_SIZE", 8):
            with self.assertRaises(aead.PlaintextTooLargeError):
                aead.seal(KEY, b"x" * 9)

    def test_fixed_rng_makes_output_deterministic(self):
        """随机数源被固定：nonce 重用导致同一明文封装结果相同。

        该用例既验证库在固定随机源下功能仍正确（可解封、可检篡改），
        也演示为何生产环境必须使用安全随机源：nonce 重用会让
        同一明文产生同一密文，泄露明文相等性。
        """
        fixed_rng = lambda n: b"\x00" * n  # noqa: E731
        packet1 = aead.seal(KEY, PLAINTEXT, AAD, rng=fixed_rng)
        packet2 = aead.seal(KEY, PLAINTEXT, AAD, rng=fixed_rng)
        self.assertEqual(packet1, packet2)  # 随机化失效，确定性输出
        plaintext, aad = aead.open(KEY, packet1)
        self.assertEqual((plaintext, aad), (PLAINTEXT, AAD))
        tampered = flip_bit(packet1, len(packet1) - 1)
        with self.assertRaises(aead.TagMismatchError):
            aead.open(KEY, tampered)

    def test_rng_returning_wrong_size_rejected(self):
        with self.assertRaises(ValueError):
            aead.seal(KEY, PLAINTEXT, rng=lambda n: b"\x00" * (n - 1))

    def test_short_master_key_rejected(self):
        with self.assertRaises(ValueError):
            aead.seal(b"short", PLAINTEXT)

    def test_aad_not_transmitted_still_verified(self):
        """AAD 在报文内传输并认证；对端解封后可直接拿到一致的 AAD。"""
        packet = aead.seal(KEY, PLAINTEXT, b"ctx-1")
        _, aad = aead.open(KEY, packet)
        self.assertEqual(aad, b"ctx-1")


if __name__ == "__main__":
    unittest.main()
