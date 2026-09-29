"""shamir.py 自测：组合枚举验证、篡改检出、边界用例。

运行：python3 test_shamir.py
  - 先执行组合枚举验证，把验证数据打印并写入 verification_data.txt
  - 再运行全部 unittest 用例
"""

import itertools
import os
import unittest

import shamir
from shamir import (
    ConsistencyError,
    InsufficientSharesError,
    InvalidShareError,
    Share,
    combine,
    split,
)

SECRET = b"master-key:0xDEADBEEF-\xe5\xaf\x86\xe9\x92\xa5"  # 含多字节 UTF-8 的主密钥


# ---------------------------------------------------------------------------
# 组合枚举验证（生成验证数据）
# ---------------------------------------------------------------------------

def enumerate_combinations(secret=SECRET, verbose=True):
    """对多组 (n, k) 枚举所有 C(n,k) 组合及若干超集，逐一重建并核对。"""
    cases = [(1, 1), (2, 1), (2, 2), (3, 1), (3, 2), (3, 3),
             (4, 2), (5, 3), (6, 4), (5, 5), (7, 4)]
    lines = []
    lines.append(f"秘密 ({len(secret)} 字节): {secret!r}")
    lines.append(f"秘密 SHA-256 前16字节: {shamir._secret_digest(secret).hex()}")
    total = 0
    for n, k in cases:
        shares = split(secret, n, k)
        combos = list(itertools.combinations(range(n), k))
        # 再补充两个超集（份额数 > k 时同样必须精确恢复）
        extra = []
        if n > k:
            extra.append(tuple(range(k + 1)))
            extra.append(tuple(range(n)))
        lines.append(f"\n(n={n}, k={k}) 共 {len(combos)} 个阈值组合"
                     f" + {len(extra)} 个超集组合：")
        for combo in combos + extra:
            picked = [shares[i] for i in combo]
            recovered = combine(picked)
            assert recovered == secret, f"组合 {combo} 恢复失败"
            xs = ",".join(str(i + 1) for i in combo)
            lines.append(f"  份额 {{{xs}}}  ({len(combo)}/{k} 份) -> 精确恢复 OK")
            total += 1
    lines.append(f"\n全部 {total} 个组合均精确恢复原密钥。")
    text = "\n".join(lines)
    if verbose:
        print(text)
    return text, total


# ---------------------------------------------------------------------------
# 有限域自检
# ---------------------------------------------------------------------------

class TestGF(unittest.TestCase):
    def test_generator_order(self):
        table = shamir._EXP[:255]
        self.assertEqual(len(set(table)), 255, "3 应是 GF(2^8) 的本原元")
        self.assertNotIn(0, table)
        self.assertEqual(shamir._EXP[255], shamir._EXP[0])

    def test_field_axioms_sampled(self):
        rng = itertools.product(range(256), repeat=2)
        for a, b in rng:
            self.assertEqual(shamir._mul(a, b), shamir._mul(b, a))  # 交换律
            if b != 0:
                self.assertEqual(shamir._mul(shamir._div(a, b), b), a)  # 乘除互逆
        for a in range(1, 256):
            self.assertEqual(shamir._mul(a, shamir._div(1, a)), 1)  # 乘法逆元
            self.assertEqual(shamir._mul(a, 1), a)
            self.assertEqual(shamir._mul(a, 0), 0)


# ---------------------------------------------------------------------------
# 组合枚举（unittest 形式，覆盖所有 C(n,k) 组合）
# ---------------------------------------------------------------------------

class TestCombinations(unittest.TestCase):
    def test_all_threshold_combinations(self):
        for n, k in [(1, 1), (3, 2), (5, 3), (6, 4), (5, 5)]:
            shares = split(SECRET, n, k)
            for combo in itertools.combinations(range(n), k):
                with self.subTest(n=n, k=k, combo=combo):
                    self.assertEqual(combine([shares[i] for i in combo]), SECRET)

    def test_supersets_also_recover(self):
        shares = split(SECRET, 5, 3)
        self.assertEqual(combine(shares[:4]), SECRET)
        self.assertEqual(combine(shares), SECRET)

    def test_random_secret_each_run(self):
        secret = os.urandom(32)
        shares = split(secret, 4, 2)
        for combo in itertools.combinations(range(4), 2):
            self.assertEqual(combine([shares[i] for i in combo]), secret)


# ---------------------------------------------------------------------------
# 篡改检出
# ---------------------------------------------------------------------------

def _flip(raw: bytes, pos: int) -> bytes:
    buf = bytearray(raw)
    buf[pos] ^= 0x01
    return bytes(buf)


class TestTamperDetection(unittest.TestCase):
    def setUp(self):
        self.shares = split(SECRET, 5, 3)
        self.raw = [s.to_bytes() for s in self.shares]

    def test_flip_byte_in_payload(self):
        bad = _flip(self.raw[0], 25)  # y 数据区
        with self.assertRaises(InvalidShareError):
            combine([bad, self.raw[1], self.raw[2]])

    def test_flip_byte_in_digest(self):
        bad = _flip(self.raw[1], 6)  # 秘密摘要区
        with self.assertRaises(InvalidShareError):
            combine([self.raw[0], bad, self.raw[2]])

    def test_flip_byte_in_checksum(self):
        bad = _flip(self.raw[2], len(self.raw[2]) - 1)  # 校验和区
        with self.assertRaises(InvalidShareError):
            combine([self.raw[0], self.raw[1], bad])

    def test_flip_every_payload_byte_detected(self):
        # 穷举：对 y 区每个字节翻转，全部必须被检出，绝不返回错误秘密
        for pos in range(20, len(self.raw[0]) - 16):
            bad = _flip(self.raw[0], pos)
            with self.assertRaises(InvalidShareError):
                combine([bad, self.raw[1], self.raw[2]])

    def test_forged_checksum_still_rejected(self):
        # 攻击者篡改 y 后重新计算校验和（绕过第一道防线）：
        # 必须被秘密摘要一致性检查拦下
        victim = Share.from_bytes(self.raw[0])
        forged = Share(victim.x, victim.threshold, victim.count,
                       victim.digest, bytes([victim.data[0] ^ 1]) + victim.data[1:])
        with self.assertRaises(ConsistencyError):
            combine([forged.to_bytes(), self.raw[1], self.raw[2]])

    def test_truncated_share(self):
        with self.assertRaises(InvalidShareError):
            combine([self.raw[0][:-10], self.raw[1], self.raw[2]])

    def test_duplicate_share_rejected(self):
        with self.assertRaises(InvalidShareError):
            combine([self.shares[0], self.shares[0], self.shares[1]])

    def test_shares_from_different_sessions(self):
        other = split(SECRET, 5, 3)  # 同一秘密、不同随机多项式
        with self.assertRaises((InvalidShareError, ConsistencyError)):
            combine([self.shares[0], other[1], other[2]])

    def test_tampered_never_returns_wrong_secret(self):
        # 综合：任何篡改场景下，combine 要么抛异常，要么返回原秘密
        for i in range(3):
            bad = _flip(self.raw[i], 20 + i)
            try:
                result = combine([self.raw[0], self.raw[1], self.raw[2]] if False
                                 else [bad if j == i else self.raw[j] for j in range(3)])
            except InvalidShareError:
                continue
            self.assertEqual(result, SECRET)


# ---------------------------------------------------------------------------
# 边界用例
# ---------------------------------------------------------------------------

class TestEdgeCases(unittest.TestCase):
    def test_threshold_1(self):
        shares = split(SECRET, 4, 1)
        for s in shares:  # 任意一份即可恢复
            self.assertEqual(combine([s]), SECRET)
        self.assertEqual(combine(shares), SECRET)

    def test_threshold_equals_n(self):
        shares = split(SECRET, 6, 6)
        self.assertEqual(combine(shares), SECRET)
        with self.assertRaises(InsufficientSharesError):
            combine(shares[:5])  # 少一份都不行

    def test_insufficient_shares(self):
        shares = split(SECRET, 5, 3)
        with self.assertRaises(InsufficientSharesError):
            combine(shares[:2])
        with self.assertRaises(InsufficientSharesError):
            combine(shares[:1])
        with self.assertRaises(InsufficientSharesError):
            combine([])

    def test_long_secret(self):
        secret = os.urandom(100_000)  # 100 KB 超长秘密
        shares = split(secret, 5, 3)
        self.assertEqual(combine([shares[0], shares[2], shares[4]]), secret)
        self.assertEqual(combine(shares), secret)

    def test_single_byte_secret(self):
        shares = split(b"\x00", 3, 2)
        self.assertEqual(combine(shares[:2]), b"\x00")

    def test_all_byte_values_secret(self):
        secret = bytes(range(256))
        shares = split(secret, 4, 3)
        self.assertEqual(combine([shares[1], shares[2], shares[3]]), secret)

    def test_empty_secret_rejected(self):
        with self.assertRaises(ValueError):
            split(b"", 3, 2)

    def test_invalid_parameters(self):
        for n, k in [(0, 0), (3, 0), (2, 3), (256, 1), (300, 2)]:
            with self.subTest(n=n, k=k), self.assertRaises(ValueError):
                split(SECRET, n, k)

    def test_max_shares_255(self):
        shares = split(b"x", 255, 2)
        self.assertEqual(combine([shares[0], shares[254]]), b"x")

    def test_serialization_roundtrip(self):
        shares = split(SECRET, 3, 2)
        hexes = [s.to_hex() for s in shares]
        self.assertEqual(combine(hexes[:2]), SECRET)  # 直接传 hex 字符串
        raws = [s.to_bytes() for s in shares]
        self.assertEqual(combine(raws[1:]), SECRET)   # 直接传 bytes

    def test_bad_hex_rejected(self):
        with self.assertRaises(InvalidShareError):
            combine(["zzzz-not-hex", "abcd"])


if __name__ == "__main__":
    print("=" * 72)
    print("组合枚举验证")
    print("=" * 72)
    text, total = enumerate_combinations()
    with open("verification_data.txt", "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(f"\n验证数据已写入 verification_data.txt（共 {total} 个组合）\n")

    print("=" * 72)
    print("单元测试（有限域 / 组合 / 篡改检出 / 边界用例）")
    print("=" * 72)
    unittest.main(argv=["test_shamir.py", "-v"], exit=False)
