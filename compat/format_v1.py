"""格式 v1（旧版本）。

文件头（40 字节，大端）：
    magic       4s   = b"EFMT"
    version     H    = 1
    key_version H    加密所用密钥的版本号
    block_size  I    = 1024
    block_count I
    data_len    Q    明文总长度
    nonce       16s
随后为 block_count 个块，每块 = 密文（<= block_size 字节）+ 32 字节 HMAC 标签。
"""

import hashlib
import hmac
import struct

from .crypto import TAG_SIZE, block_tag, xor_keystream
from .errors import Reason, RejectError
from .framework import ReadOk

MAGIC = b"EFMT"
VERSION = 1
BLOCK_SIZE = 1024
KEY_VERSION = 1
HEADER = struct.Struct(">4sHHIIQ16s")


def write_file(data, key, key_version=KEY_VERSION, nonce=None):
    if nonce is None:
        nonce = hashlib.sha256(b"efmt-nonce" + bytes([VERSION]) + data).digest()[:16]
    block_count = (len(data) + BLOCK_SIZE - 1) // BLOCK_SIZE
    header = HEADER.pack(MAGIC, VERSION, key_version, BLOCK_SIZE,
                         block_count, len(data), nonce)
    out = [header]
    for i in range(block_count):
        chunk = data[i * BLOCK_SIZE:(i + 1) * BLOCK_SIZE]
        ct = xor_keystream(key, nonce, chunk, offset=i * BLOCK_SIZE)
        out.append(ct)
        out.append(block_tag(key, header, i, ct))
    return b"".join(out)


def read_file(blob, keyring):
    if len(blob) < HEADER.size:
        raise RejectError(Reason.MALFORMED, "文件头不完整")
    magic, version, key_version, block_size, block_count, data_len, nonce = \
        HEADER.unpack_from(blob)
    if magic != MAGIC:
        raise RejectError(Reason.MALFORMED, "魔数不匹配")
    if version != VERSION:
        raise RejectError(Reason.UNSUPPORTED_VERSION,
                          "文件版本 v%d，本实现仅支持 v%d" % (version, VERSION))
    if block_size != BLOCK_SIZE:
        raise RejectError(Reason.MALFORMED,
                          "块大小 %d 与 v1 规范（%d）不符" % (block_size, BLOCK_SIZE))
    key = keyring.get(key_version)
    if key is None:
        raise RejectError(Reason.KEY_UNAVAILABLE,
                          "密钥版本 kv%d 不在密钥环中" % key_version)
    header_bytes = blob[:HEADER.size]
    plain = []
    off = HEADER.size
    for i in range(block_count):
        clen = min(block_size, data_len - i * block_size)
        if clen <= 0:
            raise RejectError(Reason.MALFORMED, "块数量与明文长度矛盾")
        ct = blob[off:off + clen]
        tag = blob[off + clen:off + clen + TAG_SIZE]
        if len(ct) != clen or len(tag) != TAG_SIZE:
            raise RejectError(Reason.MALFORMED, "文件被截断（块 %d 不完整）" % i)
        if not hmac.compare_digest(tag, block_tag(key, header_bytes, i, ct)):
            raise RejectError(Reason.INTEGRITY, "块 %d 完整性校验失败" % i)
        plain.append(xor_keystream(key, nonce, ct, offset=i * block_size))
        off += clen + TAG_SIZE
    if off != len(blob):
        raise RejectError(Reason.MALFORMED, "尾部存在多余数据")
    return ReadOk(b"".join(plain)[:data_len])
