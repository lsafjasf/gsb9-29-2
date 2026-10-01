"""HOTP (RFC 4226) / TOTP (RFC 6238) 口令生成，仅依赖标准库。

时钟可注入：所有取时间的入口都接受 ``clock`` 可调用对象（返回 Unix 秒）。
"""
from __future__ import annotations

import hashlib
import hmac
import math
import struct
import time
from typing import Callable, Hashable, Optional

# RFC 4226 §4 建议共享密钥至少 128 bit
MIN_KEY_BYTES = 16
MIN_DIGITS = 6
MAX_DIGITS = 10


def validate_key(key: bytes | str) -> bytes:
    """校验并归一化密钥，少于 16 字节（128 bit）抛 ValueError。"""
    if isinstance(key, str):
        key = key.encode("ascii")
    if not isinstance(key, (bytes, bytearray)):
        raise TypeError("key 必须是 bytes/str")
    if len(key) < MIN_KEY_BYTES:
        raise ValueError(
            "密钥长度非法：至少 %d 字节（128 bit），实际 %d 字节"
            % (MIN_KEY_BYTES, len(key))
        )
    return bytes(key)


def validate_digits(digits: int) -> int:
    if not isinstance(digits, int) or not MIN_DIGITS <= digits <= MAX_DIGITS:
        raise ValueError("digits 必须是 %d..%d 之间的整数" % (MIN_DIGITS, MAX_DIGITS))
    return digits


def counter_for(t: float, period: int = 30, t0: int = 0) -> int:
    """时间戳 -> 窗口计数器。

    边界语义（floor）：窗口 k 覆盖 [k*period, (k+1)*period)。
    因此 t 恰好等于 k*period 的瞬间属于新窗口 k，而不是旧窗口 k-1。
    t 为负数或小于 t0 时返回负值（实现一致，不建议使用）。
    """
    if period <= 0:
        raise ValueError("period 必须为正整数")
    return math.floor((t - t0) / period)


def hotp(
    key: bytes | str,
    counter: int,
    digits: int = 6,
    digest: Callable = hashlib.sha1,
) -> str:
    """RFC 4226 HOTP：HMAC 截断得到的定长十进制字符串。"""
    key = validate_key(key)
    digits = validate_digits(digits)
    if counter < 0:
        raise ValueError("counter 不能为负")
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    binary = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    otp = binary % (10 ** digits)
    return str(otp).zfill(digits)


class TOTP:
    """按时间窗口生成口令。

    参数
    ----
    key:     共享密钥（>=16 字节）
    digits:  口令位数（6..10，RFC 6238 标准向量用 8 位）
    period:  窗口长度（秒）
    t0:      起始时间（Unix 秒）
    digest:  散列构造器：hashlib.sha1 / sha256 / sha512
    clock:   注入的时钟，返回当前 Unix 秒，默认 time.time
    """

    def __init__(
        self,
        key: bytes | str,
        digits: int = 6,
        period: int = 30,
        t0: int = 0,
        digest: Callable = hashlib.sha1,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.key = validate_key(key)
        self.digits = validate_digits(digits)
        if period <= 0:
            raise ValueError("period 必须为正整数")
        self.period = period
        self.t0 = t0
        self.digest = digest
        self.clock = clock

    def counter_at(self, t: Optional[float] = None) -> int:
        if t is None:
            t = self.clock()
        return counter_for(t, self.period, self.t0)

    def at(self, t: float, counter_offset: int = 0) -> str:
        """生成 t 所在窗口（可再偏移 counter_offset 个窗口）的口令。"""
        counter = self.counter_at(t) + counter_offset
        return hotp(self.key, counter, self.digits, self.digest)

    def now(self) -> str:
        return self.at(self.clock())
