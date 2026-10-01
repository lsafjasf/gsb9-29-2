"""Shamir 门限秘密共享库（纯标准库实现）。

方案概述
========
* 有限域：GF(2^8)，不可约多项式 x^8 + x^4 + x^3 + x + 1（0x11B，与 AES 相同）。
  加/减法即按位异或；乘法用指数表/对数表实现；非零元素均可逆。
* 份额生成：秘密按字节拆分。对每个字节 s，在 GF(2^8) 上随机取
  t-1 次多项式 f(x) = s + a1*x + ... + a_{t-1}*x^{t-1}（系数用 secrets 模块的
  密码学安全随机数生成），第 i 份份额取 y_i = f(x_i)，x_i = i（1..n）。
  x = 0 保留给秘密本身，因此最多 255 份。
* 重建：任意 t 份用 GF(2^8) 上的拉格朗日插值在 x=0 处求值，逐字节恢复秘密。
  少于 t 份时，对任意候选秘密都存在同等数量的多项式通过给定点，
  信息论上无法获得秘密的任何信息。

篡改检出
========
每份份额头部携带：
* threshold / share_count：方案参数，跨份额一致性校验；
* digest：SHA-256(秘密) 承诺，所有份额必须一致；
* 恢复时对每份额单独用插值点验证 y_i == f(x_i)（份额间一致性检查），
  最后校验 SHA-256(恢复结果) == digest。
任意一处不一致即抛出 TamperedShareError，绝不返回错误密钥。
"""

from __future__ import annotations

import base64
import hashlib
import secrets as _secrets
from dataclasses import dataclass

__all__ = [
    "Share",
    "SecretSharingError",
    "InvalidParameterError",
    "InvalidShareError",
    "InsufficientSharesError",
    "TamperedShareError",
    "split_secret",
    "recover_secret",
    "MAX_SHARES",
]

# GF(2^8) 中 x=0 保留给秘密，份额下标只能取 1..255
MAX_SHARES = 255

# ---------------------------------------------------------------------------
# GF(2^8) 运算（模 0x11B）
# ---------------------------------------------------------------------------

_EXP = [0] * 512  # 指数表（重复一遍避免取模）
_LOG = [0] * 256  # 对数表


def _init_tables() -> None:
    # 模不可约多项式 x^8+x^4+x^3+x+1（0x11B，低 8 位 0x1B）。
    # 注意：该多项式下 2 的阶只有 51，不是本原元；与 AES 一样取生成元 g=3。
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        # x <- x * 3 = (x*2) ^ x，其中 x*2 需要模约减
        x2 = ((x << 1) ^ 0x11B) & 0xFF if x & 0x80 else (x << 1) & 0xFF
        x = x2 ^ x
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]



_init_tables()


def _gf_mul(a: int, b: int) -> int:
    """GF(2^8) 乘法。"""
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _gf_inv(a: int) -> int:
    """GF(2^8) 乘法逆元，a 必须非零。"""
    if a == 0:
        raise ZeroDivisionError("GF(2^8) 中 0 没有乘法逆元")
    return _EXP[255 - _LOG[a]]


def _eval_poly(coeffs: list[int], x: int) -> int:
    """Horner 法在 GF(2^8) 上求多项式在 x 处的值。"""
    y = 0
    for c in reversed(coeffs):
        y = _gf_mul(y, x) ^ c
    return y


def _interpolate_at(xs: list[int], ys: list[int], x: int) -> int:
    """拉格朗日插值，求过 (xs[i], ys[i]) 的多项式在 x 处的值。"""
    acc = 0
    for i in range(len(xs)):
        num = 1
        den = 1
        for j in range(len(xs)):
            if i == j:
                continue
            num = _gf_mul(num, x ^ xs[j])      # (x - x_j) == (x + x_j)（特征 2）
            den = _gf_mul(den, xs[i] ^ xs[j])  # (x_i - x_j) == (x_i + x_j)
        acc ^= _gf_mul(ys[i], _gf_mul(num, _gf_inv(den)))
    return acc


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class SecretSharingError(Exception):
    """所有秘密共享相关异常的基类。"""


class InvalidParameterError(SecretSharingError):
    """拆分/恢复参数非法。"""


class InvalidShareError(SecretSharingError):
    """份额格式非法（无法解析或字段越界）。"""


class InsufficientSharesError(SecretSharingError):
    """份额数量不足，未达到阈值。"""


class TamperedShareError(SecretSharingError):
    """检测到份额被篡改或份额之间不一致，拒绝恢复。"""


# ---------------------------------------------------------------------------
# 份额
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Share:
    """一份份额。

    index        份额下标 x_i，取值 1..255
    threshold    门限 t
    share_count  总份数 n
    digest       SHA-256(秘密) 的 32 字节承诺
    payload      秘密逐字节拆分得到的 y 值，长度与秘密相同
    """

    index: int
    threshold: int
    share_count: int
    digest: bytes
    payload: bytes

    def serialize(self) -> str:
        """序列化为单行文本：v1:<index>:<t>:<n>:<b64(digest)>:<b64(payload)>"""
        return "v1:{}:{}:{}:{}:{}".format(
            self.index,
            self.threshold,
            self.share_count,
            base64.b64encode(self.digest).decode("ascii"),
            base64.b64encode(self.payload).decode("ascii"),
        )

    @classmethod
    def parse(cls, text: str) -> "Share":
        """解析 serialize() 的输出，格式非法时抛 InvalidShareError。"""
        parts = text.strip().split(":")
        if len(parts) != 6 or parts[0] != "v1":
            raise InvalidShareError("份额格式非法：应为 v1:<i>:<t>:<n>:<digest>:<payload>")
        try:
            index = int(parts[1])
            threshold = int(parts[2])
            share_count = int(parts[3])
            digest = base64.b64decode(parts[4], validate=True)
            payload = base64.b64decode(parts[5], validate=True)
        except (ValueError, TypeError) as exc:
            raise InvalidShareError(f"份额字段无法解析: {exc}") from exc
        share = cls(index, threshold, share_count, digest, payload)
        share._check_fields()
        return share

    def _check_fields(self) -> None:
        if not 1 <= self.index <= MAX_SHARES:
            raise InvalidShareError(f"份额下标越界: {self.index}")
        if not 1 <= self.threshold <= MAX_SHARES:
            raise InvalidShareError(f"阈值越界: {self.threshold}")
        if not self.threshold <= self.share_count <= MAX_SHARES:
            raise InvalidShareError(
                f"份数越界: threshold={self.threshold}, share_count={self.share_count}"
            )
        if len(self.digest) != hashlib.sha256().digest_size:
            raise InvalidShareError("摘要长度不是 32 字节")


# ---------------------------------------------------------------------------
# 拆分
# ---------------------------------------------------------------------------


def split_secret(secret: bytes, threshold: int, share_count: int) -> list[Share]:
    """把 secret 拆成 share_count 份，任意 threshold 份可恢复。

    threshold == 1 时每份都携带完整秘密（1-of-n 备份）；
    threshold == share_count 时缺一不可。
    """
    if not isinstance(secret, (bytes, bytearray)):
        raise InvalidParameterError("secret 必须是 bytes")
    secret = bytes(secret)
    if not 1 <= threshold <= MAX_SHARES:
        raise InvalidParameterError(f"threshold 必须在 1..{MAX_SHARES}，得到 {threshold}")
    if not threshold <= share_count <= MAX_SHARES:
        raise InvalidParameterError(
            f"share_count 必须在 threshold..{MAX_SHARES}，得到 {share_count}"
        )

    digest = hashlib.sha256(secret).digest()
    payloads = [bytearray() for _ in range(share_count)]
    xs = list(range(1, share_count + 1))

    for byte in secret:
        # 常数项是秘密字节，其余系数密码学安全随机
        coeffs = [byte] + [_secrets.randbelow(256) for _ in range(threshold - 1)]
        for k, x in enumerate(xs):
            payloads[k].append(_eval_poly(coeffs, x))

    return [
        Share(
            index=xs[k],
            threshold=threshold,
            share_count=share_count,
            digest=digest,
            payload=bytes(payloads[k]),
        )
        for k in range(share_count)
    ]


# ---------------------------------------------------------------------------
# 恢复
# ---------------------------------------------------------------------------


def _coerce_share(share: "Share | str") -> Share:
    if isinstance(share, Share):
        share._check_fields()
        return share
    if isinstance(share, str):
        return Share.parse(share)
    raise InvalidShareError(f"无法识别的份额类型: {type(share)!r}")


def recover_secret(shares: "list[Share | str]") -> bytes:
    """用不少于 threshold 份份额恢复秘密。

    任何篡改/不一致都会抛出异常（TamperedShareError 等），
    绝不会把错误密钥当作成功结果返回。
    """
    if not shares:
        raise InsufficientSharesError("没有提供任何份额")
    parsed = [_coerce_share(s) for s in shares]

    # 跨份额一致性：threshold / share_count / digest 必须全部一致
    first = parsed[0]
    for s in parsed[1:]:
        if (s.threshold, s.share_count, s.digest) != (
            first.threshold,
            first.share_count,
            first.digest,
        ):
            raise TamperedShareError(
                "份额间参数不一致（threshold/share_count/digest 不匹配），疑似被篡改或混入了其他方案的份额"
            )

    # 下标唯一（GF(2^8) 中插值点必须互不相同）
    xs = [s.index for s in parsed]
    if len(set(xs)) != len(xs):
        raise TamperedShareError("存在重复下标的份额")

    # 载荷长度一致
    secret_len = len(first.payload)
    if any(len(s.payload) != secret_len for s in parsed):
        raise TamperedShareError("份额载荷长度不一致")

    threshold = first.threshold
    if len(parsed) < threshold:
        raise InsufficientSharesError(
            f"份额不足：需要 {threshold} 份，只提供了 {len(parsed)} 份"
        )

    points = parsed[:threshold]
    xs_t = [p.index for p in points]

    # 份额间一致性检查：多余份额必须落在同一多项式上
    for extra in parsed[threshold:]:
        for pos in range(secret_len):
            ys = [p.payload[pos] for p in points]
            if _interpolate_at(xs_t, ys, extra.index) != extra.payload[pos]:
                raise TamperedShareError(
                    f"份额 #{extra.index} 与其余份额不一致（字节 {pos}），疑似被篡改"
                )

    # 逐字节拉格朗日插值恢复
    recovered = bytearray(secret_len)
    for pos in range(secret_len):
        ys = [p.payload[pos] for p in points]
        recovered[pos] = _interpolate_at(xs_t, ys, 0)

    # 承诺校验：SHA-256(恢复结果) 必须等于份额内嵌的 digest
    if hashlib.sha256(bytes(recovered)).digest() != first.digest:
        raise TamperedShareError("恢复结果与份额内嵌的 SHA-256 摘要不一致，份额已被篡改")

    return bytes(recovered)


# ---------------------------------------------------------------------------
# 命令行接口
# ---------------------------------------------------------------------------


def _cli(argv: "list[str] | None" = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="secret_sharing",
        description="Shamir 门限秘密共享（GF(2^8)，纯标准库）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_split = sub.add_parser("split", help="拆分秘密")
    p_split.add_argument("-t", "--threshold", type=int, required=True, help="门限 t")
    p_split.add_argument("-n", "--shares", type=int, required=True, help="总份数 n")
    src = p_split.add_mutually_exclusive_group(required=True)
    src.add_argument("-s", "--secret", help="直接给出秘密文本（UTF-8）")
    src.add_argument("-i", "--input", help="从文件读取秘密（二进制）")

    p_rec = sub.add_parser("recover", help="恢复秘密")
    p_rec.add_argument("share_files", nargs="+", help="份额文件，每行一个份额")
    p_rec.add_argument("-o", "--output", help="输出文件（默认写到标准输出）")

    args = parser.parse_args(argv)

    try:
        if args.cmd == "split":
            if args.secret is not None:
                secret = args.secret.encode("utf-8")
            else:
                with open(args.input, "rb") as fh:
                    secret = fh.read()
            shares = split_secret(secret, args.threshold, args.shares)
            for share in shares:
                print(share.serialize())
        else:
            shares = []
            for path in args.share_files:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            shares.append(Share.parse(line))
            secret = recover_secret(shares)
            if args.output:
                with open(args.output, "wb") as fh:
                    fh.write(secret)
            else:
                sys.stdout.buffer.write(secret)
                sys.stdout.buffer.write(b"\n")
    except SecretSharingError as exc:
        print(f"错误: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
