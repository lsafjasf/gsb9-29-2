"""参考加密容器格式 v1 / v2 的编解码实现。

容器布局
========
v1 头（20 字节）：
    magic "ENCDEMO"(7) | version=1(1) | key_version(H,2) |
    block_size(H,2)  | block_count(Q,8)
v2 头（36 字节）：在 v1 头尾部追加 file_nonce(16)。

两个版本的头后都紧跟 32 字节 HMAC-SHA256（认证除 HMAC 外的全部头字节）。

数据块记录（两个版本相同，无填充）：
    index(Q,8) | length(Q,8) | ciphertext(length) | hmac(32)
块 HMAC 认证：version(1) || index(8) || length(8) || ciphertext。

差异
====
- v1：无 file_nonce 字段，CTR nonce 固定为 16 个 0；读取方只接受 block_size<=1024。
- v2：头内携带随机 file_nonce；读取方同时支持 v1 文件（读 v1 时报告
  needs_conversion=True，建议用 v2 重写），且接受 block_size<=4096。
"""

import hashlib
import hmac
import json
import struct
from dataclasses import dataclass
from typing import Dict, Optional

from . import crypto
from .errors import (
    DecryptError,
    IntegrityError,
    KeyNotFoundError,
    UnsupportedBlockSizeError,
    UnsupportedVersionError,
)

MAGIC = b"ENCDEMO"
V1 = 1
V2 = 2

V1_HEADER_LEN = 20
V2_HEADER_LEN = 36
BLOCK_PREFIX_LEN = 16
MAC_LEN = 32

V1_MAX_BLOCK = 1024
V2_MAX_BLOCK = 4096

_HDR_V1 = struct.Struct(">7sBHHQ")
_HDR_V2 = struct.Struct(">7sBHHQ16s")
_BLOCK_HDR = struct.Struct(">QQ")


def header_len(version: int) -> int:
    if version == V1:
        return V1_HEADER_LEN
    if version == V2:
        return V2_HEADER_LEN
    raise UnsupportedVersionError("unknown format version", "unknown_version")


def record_len(data_len: int) -> int:
    """一段长度为 data_len 的明文对应密文记录总长度。"""
    return BLOCK_PREFIX_LEN + data_len + MAC_LEN


@dataclass(frozen=True)
class ReadResult:
    """成功读取的结果。

    needs_conversion=True 表示内容可完整还原，但文件格式比读取方本版本旧，
    需要/建议转换（在本框架中对应 CONVERT，而非直接 EQUIV）。
    """

    plaintext: bytes
    version: int
    needs_conversion: bool
    block_size: int
    key_version: int
    block_count: int


class KeyRing:
    """key_version -> 32 字节主密钥。"""

    def __init__(self, keys: Optional[Dict[int, bytes]] = None):
        self._keys: Dict[int, bytes] = dict(keys or {})

    def add(self, key_version: int, key: bytes) -> "KeyRing":
        if len(key) != 32:
            raise ValueError("master key must be 32 bytes")
        self._keys[key_version] = key
        return self

    def get(self, key_version: int) -> bytes:
        try:
            return self._keys[key_version]
        except KeyError:
            raise KeyNotFoundError(
                f"no key for key_version={key_version}", "key_not_found"
            )

    def save_json(self, path) -> None:
        import base64

        payload = {
            str(kv): base64.b64encode(key).decode("ascii")
            for kv, key in sorted(self._keys.items())
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)

    @classmethod
    def load_json(cls, path) -> "KeyRing":
        import base64

        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        ring = cls()
        for kv, b64 in payload.items():
            ring.add(int(kv), base64.b64decode(b64))
        return ring


def _kdf_salt(version: int, key_version: int) -> bytes:
    return b"encdemo-kdf-v" + bytes([version, key_version & 0xFF])


def _derive(version: int, key_version: int, master: bytes):
    salt = _kdf_salt(version, key_version)
    return (
        crypto.derive_subkey(master, b"hdr-mac", salt),
        crypto.derive_subkey(master, b"enc", salt),
        crypto.derive_subkey(master, b"blk-mac", salt),
    )


def encode(
    version: int,
    master_key: bytes,
    plaintext: bytes,
    block_size: int,
    *,
    key_version: int,
    nonce: Optional[bytes] = None,
) -> bytes:
    """按指定版本编码并加密。v2 默认使用 os.urandom 的文件 nonce。"""
    if version not in (V1, V2):
        raise UnsupportedVersionError("unknown format version", "unknown_version")
    if block_size <= 0 or block_size > 65535:
        raise ValueError("block_size out of range")
    if version == V1 and block_size > V1_MAX_BLOCK:
        raise UnsupportedBlockSizeError(
            f"v1 block_size must be <= {V1_MAX_BLOCK}", "unsupported_block_size"
        )

    if version == V1:
        file_nonce = b"\x00" * 16
    else:
        if nonce is None:
            import os

            file_nonce = os.urandom(16)
        else:
            if len(nonce) != 16:
                raise ValueError("nonce must be 16 bytes")
            file_nonce = nonce

    hdr_mac_key, enc_key, blk_mac_key = _derive(version, key_version, master_key)

    block_count = (len(plaintext) + block_size - 1) // block_size if block_size else 0
    if version == V1:
        header = _HDR_V1.pack(
            MAGIC, V1, key_version, block_size, block_count
        )
    else:
        header = _HDR_V2.pack(
            MAGIC, V2, key_version, block_size, block_count, file_nonce
        )
    out = bytearray()
    out += header
    out += hmac.new(hdr_mac_key, header, hashlib.sha256).digest()

    for index in range(block_count):
        chunk = plaintext[index * block_size:(index + 1) * block_size]
        cipher = crypto.xor_keystream(enc_key, file_nonce, index, chunk)
        mac = hmac_new(
            blk_mac_key,
            bytes([version])
            + index.to_bytes(8, "big")
            + len(chunk).to_bytes(8, "big")
            + cipher,
        )
        out += _BLOCK_HDR.pack(index, len(chunk))
        out += cipher
        out += mac
    return bytes(out)



def hmac_new(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


def parse(
    blob: bytes,
    keys: KeyRing,
    *,
    allowed_versions,
    max_block_size: int,
    native_version: int,
) -> ReadResult:
    """通用解析器；各版本 Reader 通过参数限定其支持范围。"""
    if len(blob) < len(MAGIC):
        raise DecryptError("file shorter than magic", "bad_magic")
    if blob[: len(MAGIC)] != MAGIC:
        raise DecryptError("magic mismatch", "bad_magic")
    if len(blob) < len(MAGIC) + 1:
        raise DecryptError("truncated header: no version byte", "truncated_header")
    version = blob[len(MAGIC)]
    if version not in (V1, V2):
        raise UnsupportedVersionError(
            f"unknown format version {version}", "unknown_version"
        )
    if version not in allowed_versions:
        raise UnsupportedVersionError(
            f"reader does not support version {version}", "unsupported_version"
        )

    hlen = header_len(version)
    if len(blob) < hlen:
        raise DecryptError("truncated header", "truncated_header")

    if version == V1:
        magic, ver, key_version, block_size, block_count = _HDR_V1.unpack(
            blob[:hlen]
        )
        file_nonce = b"\x00" * 16
    else:
        magic, ver, key_version, block_size, block_count, file_nonce = _HDR_V2.unpack(
            blob[:hlen]
        )

    master = keys.get(key_version)
    hdr_mac_key, enc_key, blk_mac_key = _derive(version, key_version, master)
    expected_hdr_mac = hmac_new(hdr_mac_key, blob[:hlen])
    if not hmac.compare_digest(expected_hdr_mac, blob[hlen:hlen + MAC_LEN]):
        raise IntegrityError("header HMAC mismatch", "header_mac_mismatch")

    if block_size == 0 or block_size > max_block_size:
        raise UnsupportedBlockSizeError(
            f"block_size={block_size} not supported by v{native_version} reader "
            f"(max {max_block_size})",
            "unsupported_block_size",
        )

    pos = hlen + MAC_LEN
    pieces = []
    for index in range(block_count):
        if pos + BLOCK_PREFIX_LEN > len(blob):
            raise IntegrityError(
                f"truncated block header at block {index}", "truncated"
            )
        got_index, length = _BLOCK_HDR.unpack(blob[pos:pos + BLOCK_PREFIX_LEN])
        pos += BLOCK_PREFIX_LEN
        if got_index != index:
            raise IntegrityError(
                f"block index out of order: expected {index}, got {got_index}",
                "block_index_mismatch",
            )
        if length > block_size:
            raise IntegrityError(
                f"block {index} length {length} exceeds declared block_size",
                "block_too_large",
            )
        if pos + length + MAC_LEN > len(blob):
            raise IntegrityError(f"truncated block {index}", "truncated")
        cipher = blob[pos:pos + length]
        mac = blob[pos + length:pos + length + MAC_LEN]
        pos += length + MAC_LEN

        mac_input = (
            bytes([version])
            + index.to_bytes(8, "big")
            + length.to_bytes(8, "big")
            + bytes(cipher)
        )
        if not hmac.compare_digest(hmac_new(blk_mac_key, mac_input), mac):
            raise IntegrityError(
                f"block {index} HMAC mismatch (tampered or wrong key)",
                "block_mac_mismatch",
            )
        pieces.append(crypto.xor_keystream(enc_key, file_nonce, index, bytes(cipher)))

    if pos != len(blob):
        raise IntegrityError(
            f"{len(blob) - pos} trailing byte(s) after declared blocks",
            "trailing_data",
        )

    return ReadResult(
        plaintext=b"".join(pieces),
        version=version,
        needs_conversion=(version != native_version),
        block_size=block_size,
        key_version=key_version,
        block_count=block_count,
    )


class Reader:
    """某一版本程序的读取器。"""

    def __init__(self, version: int, keys: KeyRing):
        if version == V1:
            allowed, max_bs = (V1,), V1_MAX_BLOCK
        elif version == V2:
            allowed, max_bs = (V1, V2), V2_MAX_BLOCK
        else:
            raise UnsupportedVersionError("unknown reader version", "unknown_version")
        self.version = version
        self.keys = keys
        self._allowed = allowed
        self._max_bs = max_bs

    @classmethod
    def v1(cls, keys: KeyRing) -> "Reader":
        return cls(V1, keys)

    @classmethod
    def v2(cls, keys: KeyRing) -> "Reader":
        return cls(V2, keys)

    def read(self, blob: bytes) -> ReadResult:
        return parse(
            blob,
            self.keys,
            allowed_versions=self._allowed,
            max_block_size=self._max_bs,
            native_version=self.version,
        )


class Writer:
    """某一版本程序的写入器。"""

    def __init__(self, version: int, key: bytes, block_size: Optional[int] = None, *,
                 key_version: int):
        if version == V1:
            block_size = block_size or 1024
        elif version == V2:
            block_size = block_size or 2048
        else:
            raise UnsupportedVersionError("unknown writer version", "unknown_version")
        self.version = version
        self.key = key
        self.block_size = block_size
        self.key_version = key_version

    @classmethod
    def v1(cls, key: bytes, block_size: int = 1024, *, key_version: int = 1) -> "Writer":
        return cls(V1, key, block_size, key_version=key_version)

    @classmethod
    def v2(cls, key: bytes, block_size: int = 2048, *, key_version: int = 2) -> "Writer":
        return cls(V2, key, block_size, key_version=key_version)

    def write(self, plaintext: bytes, *, nonce: Optional[bytes] = None) -> bytes:
        return encode(
            self.version,
            self.key,
            plaintext,
            self.block_size,
            key_version=self.key_version,
            nonce=nonce,
        )
