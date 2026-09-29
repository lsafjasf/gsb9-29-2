"""消息认证（HMAC-SHA-256）与密钥派生（HKDF-SHA-256）。

仅使用 Python 标准库。HMAC 按 RFC 2104 手工实现（双重散列结构天然
免疫长度扩展攻击），HKDF 按 RFC 5869 实现，派生接口强制携带上下文
标签，不同上下文必然得到不同子密钥。

密钥材料用 KeyBytes（bytearray 封装）持有，支持显式 wipe()。
"""

from __future__ import annotations

import hashlib
import hmac as _stdlib_hmac

SHA256_BLOCK_SIZE = 64   # SHA-256 分组长度（字节）
SHA256_OUTPUT_SIZE = 32  # SHA-256 输出长度（字节）
HKDF_MAX_LENGTH = 255 * SHA256_OUTPUT_SIZE  # RFC 5869 单次派生上限


class KeyBytes:
    """可变字节容器，用于持有密钥材料，支持显式清零。

    Python 的 bytes 不可变，无法就地清除；bytearray 可以。
    本类把密钥放在 bytearray 里，wipe() 将其逐字节覆写为 0，
    并支持 with 语句在退出作用域时自动清除。

    语言层面的限制（务必知悉）：
    - 解释器无法保证清除"曾经出现过"的所有副本：参数传递、序列化、
      垃圾回收前的临时对象都可能残留副本；
    - 交换分区（swap）、核心转储可能把密钥写到磁盘；
    - bytes/str 一旦由密钥派生（如 hexdigest 的中间产物），
      其内容只能等 GC，无法主动擦除。
    因此 wipe() 是"尽力而为"的纵深防御，不是形式化保证。
    """

    __slots__ = ("_buf", "_wiped")

    def __init__(self, data):
        self._buf = bytearray(data)
        self._wiped = False

    def __len__(self):
        return len(self._buf)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.wipe()

    def __del__(self):  # 尽力而为；解释器退出时不保证调用
        try:
            self.wipe()
        except Exception:
            pass

    @property
    def wiped(self):
        return self._wiped

    def bytes(self):
        """取出只读副本供原语使用；调用方应缩短该副本的生存期。"""
        if self._wiped:
            raise ValueError("key material has been wiped")
        return bytes(self._buf)

    def wipe(self):
        """把底层缓冲区逐字节覆写为 0。"""
        for i in range(len(self._buf)):
            self._buf[i] = 0
        self._wiped = True


def _key_bytes(key):
    if isinstance(key, KeyBytes):
        return key.bytes()
    return bytes(key)


def hmac_sha256(key, message):
    """按 RFC 2104 计算 HMAC-SHA-256。

    结构为 H((K' ^ opad) || H((K' ^ ipad) || message))。内外两次散列
    使攻击者无法在不知道密钥的情况下，由 HMAC(K, m) 推出
    HMAC(K, m || padding || m')，即免疫长度扩展攻击——这是裸
    H(K || m) 构造所不具备的性质。
    """
    k = _key_bytes(key)
    if len(k) > SHA256_BLOCK_SIZE:
        k = hashlib.sha256(k).digest()
    k = k.ljust(SHA256_BLOCK_SIZE, b"\x00")

    ipad = bytes(b ^ 0x36 for b in k)
    opad = bytes(b ^ 0x5C for b in k)

    inner = hashlib.sha256(ipad)
    inner.update(message)
    outer = hashlib.sha256(opad)
    outer.update(inner.digest())
    return outer.digest()


def hmac_verify(key, message, tag):
    """常数时间比较认证值，避免时序侧信道。"""
    expected = hmac_sha256(key, message)
    return _stdlib_hmac.compare_digest(expected, tag)


def hkdf_extract(salt, ikm):
    """HKDF-Extract：从输入密钥材料中萃取出伪随机密钥（PRK）。"""
    if not salt:
        salt = b"\x00" * SHA256_OUTPUT_SIZE
    return hmac_sha256(salt, _key_bytes(ikm))


def hkdf_expand(prk, info, length):
    """HKDF-Expand：把 PRK 扩展成 length 字节的输出密钥材料。

    info 即上下文标签：它进入每一轮 HMAC 的输入，因此不同的
    info 必然产生不同的输出流，实现上下文隔离。
    """
    if not 0 <= length <= HKDF_MAX_LENGTH:
        raise ValueError(
            "length must be in [0, %d], got %d" % (HKDF_MAX_LENGTH, length))
    okm = bytearray()
    previous = b""
    counter = 1
    while len(okm) < length:
        previous = hmac_sha256(prk, previous + info + bytes([counter]))
        okm += previous
        counter += 1
    return bytes(okm[:length])


def hkdf(ikm, *, salt=b"", info, length):
    """完整的 HKDF = Extract + Expand。"""
    return hkdf_expand(hkdf_extract(salt, ikm), info, length)


def _encode_labeled(label, context):
    """把 (label, context) 做无歧义的长度前缀编码。

    防止拼接歧义：("ab", "c") 与 ("a", "bc") 若直接拼接会得到相同
    字节串，长度前缀编码保证二者输入不同、派生密钥也不同。
    """
    return (len(label).to_bytes(4, "big") + label
            + len(context).to_bytes(4, "big") + context)


def derive_key(master, *, label, context=b"", salt=b"",
               length=SHA256_OUTPUT_SIZE):
    """从主密钥派生一把带用途标签与上下文的子密钥。

    - label：用途标签，如 b"encryption" / b"mac" / b"backup"，
      不同用途必须不同标签，保证跨用途隔离；
    - context：业务上下文，如用户 ID、会话 ID、版本号；
    - 返回值包在 KeyBytes 中，用完可 wipe()。
    """
    info = _encode_labeled(label, context)
    return KeyBytes(hkdf(master, salt=salt, info=info, length=length))
