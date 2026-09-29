"""Shamir 门限秘密共享（仅 Python 标准库）。

有限域：GF(2^8)，不可约多项式 x^8 + x^4 + x^3 + x + 1（0x11B，与 AES 相同），
本原元（生成元）取 3。域元素为 0..255 的字节：
  - 加法 / 减法：按位异或（特征 2 的域中二者相同）；
  - 乘法 / 除法：查 exp/log 表，exp[i] = 3^i，log 为其逆表；
    a * b = exp[log[a] + log[b]]，a / b = exp[(log[a] - log[b]) mod 255]。

份额生成（split）：把秘密按字节拆开，对每个字节 s 独立构造
GF(2^8) 上的随机多项式
    f(X) = s + a1*X + a2*X^2 + ... + a_{k-1}*X^{k-1}
其中常数项为秘密字节，其余 k-1 个系数取自密码学安全随机源 os.urandom。
第 i 份份额为点 (x_i, f(x_i))，x_i = 1, 2, ..., n（x 绝不取 0，否则直接暴露常数项）。

重建（combine）：任意 >= k 份份额，用 GF(2^8) 上的拉格朗日插值在 X=0 处求值：
    s = sum_i  y_i * prod_{j != i}  x_j / (x_i - x_j)
（域中减法即异或）。少于 k 份时，常数项在信息论意义上均匀随机，无法恢复。

篡改检测（两道防线）：
  1. 每份份额尾部带 SHA-256 校验和（16 字节），解析时校验，任何比特翻转都会被拒收；
  2. 每份份额头部嵌入秘密的 SHA-256 摘要（16 字节）。重建后重新计算秘密摘要并比对，
     即使攻击者篡改份额后重新伪造校验和，重建结果也无法通过一致性检查，
     绝不会把错误密钥当作成功返回。
"""

from __future__ import annotations

import hashlib
import hmac
import os

__all__ = [
    "Share",
    "split",
    "combine",
    "ShamirError",
    "InvalidShareError",
    "InsufficientSharesError",
    "ConsistencyError",
]

_VERSION = 1
_DIGEST_LEN = 16    # 秘密摘要长度（SHA-256 截断）
_CHECKSUM_LEN = 16  # 份额校验和长度（SHA-256 截断）
_HEADER_LEN = 4     # version | x | threshold | count
_MAX_SHARES = 255   # x 坐标为 1..255，0 保留给秘密本身


# ---------------------------------------------------------------------------
# GF(2^8) 运算
# ---------------------------------------------------------------------------

_EXP = [0] * 512  # 扩展一倍以免取模
_LOG = [0] * 256


def _init_tables() -> None:
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        # x *= 3（本原元）：x*3 = x*2 ^ x
        x2 = x << 1
        if x2 & 0x100:
            x2 ^= 0x11B
        x = (x2 & 0xFF) ^ x
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_init_tables()


def _mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _div(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionError("GF(2^8) division by zero")
    if a == 0:
        return 0
    return _EXP[(_LOG[a] - _LOG[b]) % 255]


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class ShamirError(Exception):
    """秘密共享相关错误的基类。"""


class InvalidShareError(ShamirError):
    """份额格式非法、校验和失败或份额之间不一致。"""


class InsufficientSharesError(ShamirError):
    """份额数量不足，未达到阈值。"""


class ConsistencyError(ShamirError):
    """重建结果未通过秘密摘要一致性检查（份额被篡改或伪造）。"""


# ---------------------------------------------------------------------------
# 份额
# ---------------------------------------------------------------------------

class Share:
    """一份份额。

    二进制布局：
        [0]        版本号（=1）
        [1]        x 坐标（1..255）
        [2]        阈值 k
        [3]        份额总数 n
        [4:20]     秘密摘要（SHA-256 前 16 字节，同一批份额相同）
        [20:20+L]  y 值（L = 秘密长度）
        [20+L:]    校验和（对以上全部字节的 SHA-256 前 16 字节）
    """

    __slots__ = ("x", "threshold", "count", "digest", "data")

    def __init__(self, x: int, threshold: int, count: int, digest: bytes, data: bytes):
        self.x = x
        self.threshold = threshold
        self.count = count
        self.digest = bytes(digest)
        self.data = bytes(data)

    def __repr__(self) -> str:
        return (
            f"Share(x={self.x}, k={self.threshold}, n={self.count}, "
            f"len={len(self.data)})"
        )

    # -- 序列化 -----------------------------------------------------------

    def to_bytes(self) -> bytes:
        body = (
            bytes([_VERSION, self.x, self.threshold, self.count])
            + self.digest
            + self.data
        )
        return body + hashlib.sha256(body).digest()[:_CHECKSUM_LEN]

    def to_hex(self) -> str:
        return self.to_bytes().hex()

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Share":
        raw = bytes(raw)
        min_len = _HEADER_LEN + _DIGEST_LEN + 1 + _CHECKSUM_LEN
        if len(raw) < min_len:
            raise InvalidShareError(
                f"份额长度不足：{len(raw)} 字节（至少 {min_len} 字节）"
            )
        body, checksum = raw[:-_CHECKSUM_LEN], raw[-_CHECKSUM_LEN:]
        expect = hashlib.sha256(body).digest()[:_CHECKSUM_LEN]
        if not hmac.compare_digest(checksum, expect):
            raise InvalidShareError("份额校验和失败：数据被篡改或损坏")
        if body[0] != _VERSION:
            raise InvalidShareError(f"不支持的份额版本：{body[0]}")
        x, threshold, count = body[1], body[2], body[3]
        if not 1 <= x <= _MAX_SHARES:
            raise InvalidShareError(f"非法的 x 坐标：{x}")
        if not 1 <= threshold <= count <= _MAX_SHARES:
            raise InvalidShareError(
                f"非法的门限参数：k={threshold}, n={count}"
            )
        digest = body[_HEADER_LEN : _HEADER_LEN + _DIGEST_LEN]
        data = body[_HEADER_LEN + _DIGEST_LEN :]
        return cls(x, threshold, count, digest, data)

    @classmethod
    def from_hex(cls, text: str) -> "Share":
        try:
            raw = bytes.fromhex(text.strip())
        except ValueError as exc:
            raise InvalidShareError(f"份额不是合法十六进制：{exc}") from exc
        return cls.from_bytes(raw)


# ---------------------------------------------------------------------------
# 拆分 / 重建
# ---------------------------------------------------------------------------

def _secret_digest(secret: bytes) -> bytes:
    return hashlib.sha256(secret).digest()[:_DIGEST_LEN]


def split(secret: bytes, n: int, k: int) -> list[Share]:
    """把 secret 拆成 n 份，任意 k 份可重建（(k, n) 门限方案）。"""
    if isinstance(secret, (bytearray, memoryview)):
        secret = bytes(secret)
    if not isinstance(secret, bytes):
        raise TypeError("secret 必须是 bytes")
    if len(secret) == 0:
        raise ValueError("秘密不能为空")
    if not (1 <= k <= n <= _MAX_SHARES):
        raise ValueError(f"参数需满足 1 <= k <= n <= {_MAX_SHARES}，收到 k={k}, n={n}")

    digest = _secret_digest(secret)
    xs = list(range(1, n + 1))
    ys = [bytearray(len(secret)) for _ in range(n)]

    for pos in range(len(secret)):
        # 随机多项式：常数项 = 秘密字节，其余 k-1 个系数随机
        coeffs = bytes([secret[pos]]) + os.urandom(k - 1)
        for si in range(n):
            x = xs[si]
            # Horner 法求 f(x)
            y = 0
            for c in reversed(coeffs):
                y = _mul(y, x) ^ c
            ys[si][pos] = y

    return [Share(xs[i], k, n, digest, bytes(ys[i])) for i in range(n)]


def _coerce(share) -> Share:
    if isinstance(share, Share):
        return share
    if isinstance(share, str):
        return Share.from_hex(share)
    if isinstance(share, (bytes, bytearray, memoryview)):
        return Share.from_bytes(bytes(share))
    raise TypeError(f"无法识别的份额类型：{type(share)!r}")


def combine(shares) -> bytes:
    """用 >= k 份份额重建秘密。

    份额不足、份额被篡改、份额不属于同一批，都会抛出异常，
    绝不会返回错误的秘密。
    """
    shares = [_coerce(s) for s in shares]
    if not shares:
        raise InsufficientSharesError("没有提供任何份额")

    first = shares[0]
    k, n, digest = first.threshold, first.count, first.digest
    length = len(first.data)

    seen_x = set()
    for s in shares:
        if s.threshold != k or s.count != n:
            raise InvalidShareError("份额的门限参数不一致（不是同一批份额）")
        if s.digest != digest or len(s.data) != length:
            raise InvalidShareError("份额的秘密摘要不一致（不是同一批份额）")
        if s.x in seen_x:
            raise InvalidShareError(f"x 坐标重复：{s.x}（同一份份额被重复使用）")
        seen_x.add(s.x)

    if len(shares) < k:
        raise InsufficientSharesError(
            f"份额不足：需要 {k} 份，只有 {len(shares)} 份"
        )

    # 拉格朗日插值在 X=0 处的系数（只与 x 坐标有关，与字节位置无关，先算一次）
    xs = [s.x for s in shares]
    lambdas = []
    for i, xi in enumerate(xs):
        num, den = 1, 1
        for j, xj in enumerate(xs):
            if i == j:
                continue
            num = _mul(num, xj)          # (0 - x_j) = x_j
            den = _mul(den, xi ^ xj)     # (x_i - x_j) = x_i ^ x_j
        lambdas.append(_div(num, den))

    out = bytearray(length)
    datas = [s.data for s in shares]
    for pos in range(length):
        acc = 0
        for si in range(len(shares)):
            acc ^= _mul(datas[si][pos], lambdas[si])
        out[pos] = acc
    secret = bytes(out)

    # 一致性检查：重建结果必须与原秘密摘要匹配
    if not hmac.compare_digest(_secret_digest(secret), digest):
        raise ConsistencyError(
            "重建结果未通过秘密摘要校验：份额被篡改或伪造，拒绝返回"
        )
    return secret
