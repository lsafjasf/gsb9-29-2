"""格式 v2（新版本）。

相对 v1 的变化：
1. 块大小 1024 -> 4096（块大小变化场景）；
2. 文件头新增 flags 字段（布局变化，旧程序若不检查版本号会解析错位）。

v2 读取器向后兼容：可读取 v1 文件，在内存中按 v2 块布局重切分，
因此返回 ReadOk(converted=True)。

文件头（44 字节，大端）：
    magic 4s | version H=2 | key_version H | block_size I=4096
    block_count I | data_len Q | flags I | nonce 16s
"""

import hashlib
import hmac
import struct

from . import format_v1
from .crypto import TAG_SIZE, block_tag, xor_keystream
from .errors import Reason, RejectError
from .framework import ReadOk

MAGIC = b"EFMT"
VERSION = 2
BLOCK_SIZE = 4096
KEY_VERSION = 2
HEADER = struct.Struct(">4sHHIIQI16s")


def write_file(data, key, key_version=KEY_VERSION, nonce=None, target_version=2,
               flags=0):
    """target_version=1 时降级写出 v1 格式文件（跨版本写入场景）。"""
    if target_version == 1:
        return format_v1.write_file(data, key, key_version=format_v1.KEY_VERSION)
    if nonce is None:
        nonce = hashlib.sha256(b"efmt-nonce" + bytes([VERSION]) + data).digest()[:16]
    block_count = (len(data) + BLOCK_SIZE - 1) // BLOCK_SIZE
    header = HEADER.pack(MAGIC, VERSION, key_version, BLOCK_SIZE,
                         block_count, len(data), flags, nonce)
    out = [header]
    for i in range(block_count):
        chunk = data[i * BLOCK_SIZE:(i + 1) * BLOCK_SIZE]
        ct = xor_keystream(key, nonce, chunk, offset=i * BLOCK_SIZE)
        out.append(ct)
        out.append(block_tag(key, header, i, ct))
    return b"".join(out)


def _read_v2(blob, keyring):
    if len(blob) < HEADER.size:
        raise RejectError(Reason.MALFORMED, "文件头不完整")
    (magic, version, key_version, block_size, block_count,
     data_len, flags, nonce) = HEADER.unpack_from(blob)
    if block_size != BLOCK_SIZE:
        raise RejectError(Reason.MALFORMED,
                          "块大小 %d 与 v2 规范（%d）不符" % (block_size, BLOCK_SIZE))
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


def read_file(blob, keyring):
    if len(blob) < 6:
        raise RejectError(Reason.MALFORMED, "文件头不完整")
    magic, version = struct.unpack_from(">4sH", blob)
    if magic != MAGIC:
        raise RejectError(Reason.MALFORMED, "魔数不匹配")
    if version == 1:
        res = format_v1.read_file(blob, keyring)
        return ReadOk(res.plaintext, converted=True,
                      note="v1 文件（块 1024）已按 v2 块布局（4096）重切分读取")
    if version == VERSION:
        return _read_v2(blob, keyring)
    raise RejectError(Reason.UNSUPPORTED_VERSION,
                      "文件版本 v%d，本实现支持 v1/v2" % version)
