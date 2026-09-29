"""标准库加密原语（HKDF-SHA256、HMAC-SHA256、基于 HMAC 的密钥流）。

安全声明：HKDF/HMAC 是标准构造；但本参考格式用 HMAC(块密钥, nonce||计数器)
逐块生成密钥流来模拟流密码，仅用于兼容性测试演示，请勿用于生产数据保护。
"""

import hashlib
import hmac

HASH_LEN = 32


def hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    if not salt:
        salt = b"\x00" * HASH_LEN
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    if length > 255 * HASH_LEN:
        raise ValueError("hkdf_expand: length too large")
    out = bytearray()
    t = b""
    counter = 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([counter]), hashlib.sha256).digest()
        out.extend(t)
        counter += 1
    return bytes(out[:length])


def hkdf(ikm: bytes, salt: bytes, info: bytes, length: int = HASH_LEN) -> bytes:
    return hkdf_expand(hkdf_extract(salt, ikm), info, length)


def derive_subkey(master_key: bytes, purpose: bytes, salt: bytes) -> bytes:
    """派生单个用途子密钥。salt 必须绑定文件版本与密钥版本。"""
    return hkdf(master_key, salt, purpose, HASH_LEN)


def xor_keystream(key: bytes, nonce: bytes, counter: int, data: bytes) -> bytes:
    """以 HMAC-SHA256(key, nonce || be64(counter)) 生成密钥流做 CTR 异或。

    每次扩展一个计数器值产生 32 字节密钥流，足以覆盖演示用块大小。
    """
    if len(nonce) != 16:
        raise ValueError("nonce must be 16 bytes")
    out = bytearray(len(data))
    offset = 0
    ctr = counter
    mv = memoryview(data)
    while offset < len(data):
        block = hmac.new(
            key, nonce + ctr.to_bytes(8, "big"), hashlib.sha256
        ).digest()
        chunk = mv[offset:offset + len(block)]
        out[offset:offset + len(chunk)] = bytes(a ^ b for a, b in zip(chunk, block))
        offset += len(block)
        ctr += 1
    return bytes(out)
