"""认证加密封装库（Encrypt-then-MAC），仅依赖 Python 标准库。

设计要点
========
* 组合方式：先加密后认证（Encrypt-then-MAC）。
  接收方先校验 HMAC 标签，通过后才解密，篡改的密文在进入解密
   逻辑之前就被拒绝，从根本上避免填充预言机 / 解密预言机类攻击。
* 加密：以 HMAC-SHA256 作为 PRF 的计数器模式流加密
  （keystream_i = HMAC(enc_key, nonce || counter_i)），
  每次封装使用随机 nonce，保证同一明文两次封装结果不同。
* 认证：HMAC-SHA256(mac_key, header || ciphertext)，
  头部（版本号、AAD 长度、关联数据 AAD、密文长度、nonce）
  全部纳入认证范围。
* 密钥分离：enc_key / mac_key 由主密钥经 HMAC 派生，互不通用。
* 防重排：所有字段采用定长或长度前缀的规范编码，且长度字段本身
  被 MAC 覆盖；任何字段调换都会破坏长度一致性或标签校验。

报文格式（字节序均为大端）::

    VERSION   (1)   协议版本，当前为 1
    AAD_LEN   (4)   关联数据长度
    AAD       (变长) 关联数据（只认证不加密，如头部、上下文）
    CT_LEN    (8)   密文长度（等于明文长度）
    NONCE     (16)  随机数
    CIPHERTEXT(变长) 密文
    TAG       (32)  HMAC-SHA256 标签，覆盖以上全部字段
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
from typing import Callable, Optional, Tuple

VERSION = 1
NONCE_SIZE = 16
TAG_SIZE = 32
AAD_LEN_SIZE = 4
CT_LEN_SIZE = 8
# 明文上限：防止计数器空间被耗尽以及异常的内存消耗
MAX_PLAINTEXT_SIZE = 1 << 31
MIN_MASTER_KEY_SIZE = 16

_FIXED_HEADER_SIZE = 1 + AAD_LEN_SIZE  # VERSION + AAD_LEN
_MIN_PACKET_SIZE = (
    _FIXED_HEADER_SIZE + CT_LEN_SIZE + NONCE_SIZE + TAG_SIZE
)


class AEADError(Exception):
    """所有认证加密封装错误的基类。"""


class TruncatedDataError(AEADError):
    """报文被截断：实际长度小于头部声明的长度。"""


class MalformedPacketError(AEADError):
    """报文结构非法：版本未知、长度字段矛盾或存在尾部垃圾。"""


class TagMismatchError(AEADError):
    """认证标签校验失败：密文、头部或关联数据被篡改，或字段被重排。"""


class PlaintextTooLargeError(AEADError):
    """明文超过允许的最大长度。"""


def _derive_keys(master_key: bytes) -> Tuple[bytes, bytes]:
    if len(master_key) < MIN_MASTER_KEY_SIZE:
        raise ValueError(
            f"主密钥至少需要 {MIN_MASTER_KEY_SIZE} 字节"
        )
    enc_key = hmac.new(master_key, b"aead/enc/v1", hashlib.sha256).digest()
    mac_key = hmac.new(master_key, b"aead/mac/v1", hashlib.sha256).digest()
    return enc_key, mac_key


def _keystream(enc_key: bytes, nonce: bytes, nbytes: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < nbytes:
        block = hmac.new(
            enc_key, nonce + struct.pack(">Q", counter), hashlib.sha256
        ).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:nbytes])


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def seal(
    master_key: bytes,
    plaintext: bytes,
    aad: bytes = b"",
    rng: Optional[Callable[[int], bytes]] = None,
) -> bytes:
    """封装明文，返回认证加密报文。

    :param master_key: 主密钥（>=16 字节，建议 32 字节随机数）。
    :param plaintext:  待保护的明文，可为空。
    :param aad:        关联数据（头部等），只认证不加密。
    :param rng:        随机数源，默认 os.urandom；注入固定值源可用于
                       测试，但会导致 nonce 重用，生产环境禁止。
    """
    if rng is None:
        rng = os.urandom
    if len(plaintext) > MAX_PLAINTEXT_SIZE:
        raise PlaintextTooLargeError(
            f"明文长度 {len(plaintext)} 超过上限 {MAX_PLAINTEXT_SIZE}"
        )
    enc_key, mac_key = _derive_keys(master_key)
    nonce = rng(NONCE_SIZE)
    if len(nonce) != NONCE_SIZE:
        raise ValueError(f"随机数源必须返回 {NONCE_SIZE} 字节")
    ciphertext = _xor(plaintext, _keystream(enc_key, nonce, len(plaintext)))
    header = (
        bytes([VERSION])
        + struct.pack(">I", len(aad))
        + aad
        + struct.pack(">Q", len(ciphertext))
        + nonce
    )
    tag = hmac.new(mac_key, header + ciphertext, hashlib.sha256).digest()
    return header + ciphertext + tag


def open(master_key: bytes, packet: bytes) -> Tuple[bytes, bytes]:
    """解封报文，返回 (明文, 关联数据)。

    任何篡改都会抛出 AEADError 的具体子类，绝不返回部分结果。
    """
    enc_key, mac_key = _derive_keys(master_key)

    if len(packet) < _MIN_PACKET_SIZE:
        raise TruncatedDataError(
            f"报文长度 {len(packet)} 小于最小合法长度 {_MIN_PACKET_SIZE}"
        )

    version = packet[0]
    if version != VERSION:
        raise MalformedPacketError(f"未知协议版本 {version}")

    (aad_len,) = struct.unpack(">I", packet[1 : 1 + AAD_LEN_SIZE])
    ct_len_offset = _FIXED_HEADER_SIZE + aad_len
    if len(packet) < ct_len_offset + CT_LEN_SIZE + NONCE_SIZE + TAG_SIZE:
        raise TruncatedDataError("报文在 AAD 声明的长度处被截断")
    aad = packet[_FIXED_HEADER_SIZE:ct_len_offset]
    (ct_len,) = struct.unpack(
        ">Q", packet[ct_len_offset : ct_len_offset + CT_LEN_SIZE]
    )
    nonce_offset = ct_len_offset + CT_LEN_SIZE
    nonce = packet[nonce_offset : nonce_offset + NONCE_SIZE]

    expected_total = nonce_offset + NONCE_SIZE + ct_len + TAG_SIZE
    if len(packet) < expected_total:
        raise TruncatedDataError(
            f"报文被截断：期望 {expected_total} 字节，实际 {len(packet)} 字节"
        )
    if len(packet) > expected_total:
        raise MalformedPacketError(
            f"长度字段与实际不符：期望 {expected_total} 字节，"
            f"实际 {len(packet)} 字节（存在尾部垃圾或字段被重排）"
        )

    ciphertext = packet[nonce_offset + NONCE_SIZE : -TAG_SIZE]
    tag = packet[-TAG_SIZE:]
    expected_tag = hmac.new(
        mac_key, packet[:-TAG_SIZE], hashlib.sha256
    ).digest()
    if not hmac.compare_digest(tag, expected_tag):
        raise TagMismatchError(
            "认证标签校验失败：密文/头部/关联数据被篡改或字段被重排"
        )

    plaintext = _xor(
        ciphertext, _keystream(enc_key, nonce, len(ciphertext))
    )
    return plaintext, aad


def generate_key() -> bytes:
    """生成 32 字节随机主密钥。"""
    return os.urandom(32)
