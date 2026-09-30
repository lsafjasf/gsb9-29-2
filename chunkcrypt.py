"""chunkcrypt: 分块加密文件格式（仅依赖 Python 标准库）。

格式布局（小端）::

    +------------------ 头部 ------------------+
    | magic            8B   b"CCRYPT01"        |
    | chunk_size       4B   uint32             |
    | total_size       8B   uint64 (明文总长)  |
    | chunk_count      8B   uint64             |
    | kdf_iterations   4B   uint32 (0=直接密钥)|
    | salt            16B   PBKDF2 盐          |
    | nonce           16B   文件级随机 nonce   |
    | header_mac      32B   以上所有字段的 HMAC|
    +------------------ 索引区 -----------------+
    | index            chunk_count * 32B       |
    |                  每项 = 对应密文块的 HMAC|
    | index_mac       32B   整个索引区的 HMAC  |
    +------------------ 数据区 -----------------+
    | chunk[0] .. chunk[N-1]  密文与明文等长   |
    +------------------------------------------+

加密原语（标准库内置 HMAC-SHA256 作为 PRF）:

* 保密性: PRF-CTR 流加密。块 i 第 j 个 32 字节密钥流为
  SHA-256(enc_key || nonce || i || j)，与明文异或（enc_key 是经 HMAC
  域分离派生的秘密密钥，SHA-256(secret||·) 作 PRF）。流密码 => 密文与
  明文等长，且支持块内任意偏移解密：随机读取时只为所需切片生成密钥流。
* 完整性: chunk_mac[i] = HMAC(mac_key, b"chunk" || i || ciphertext[i])，
  篡改任意一块可定位到块号；索引与头部也各有 HMAC，防止整块删除、
  重排或元数据篡改。
* 密钥: 32 字节随机主密钥，或由口令经 PBKDF2-HMAC-SHA256 派生；
  enc_key / mac_key 由主密钥经 HMAC 域分离派生。
"""

from __future__ import annotations

import hashlib
import hmac
import io
import os
import struct

MAGIC = b"CCRYPT01"
SALT_SIZE = 16
NONCE_SIZE = 16
MAC_SIZE = 32
KEY_SIZE = 32
DEFAULT_CHUNK_SIZE = 1 << 20  # 1 MiB
DEFAULT_KDF_ITERATIONS = 200_000

_HEADER_STRUCT = struct.Struct("<8sIQQI")  # magic, chunk_size, total, count, kdf_iter
HEADER_SIZE = _HEADER_STRUCT.size + SALT_SIZE + NONCE_SIZE + MAC_SIZE
INDEX_MAC_SIZE = MAC_SIZE


class ChunkCryptError(Exception):
    """格式或密钥相关错误的基类。"""


class IntegrityError(ChunkCryptError):
    """头部 / 索引完整性校验失败。"""


class ChunkTamperedError(ChunkCryptError):
    """某一块的 HMAC 校验失败，携带块号。"""

    def __init__(self, chunk_index: int):
        self.chunk_index = chunk_index
        super().__init__(f"chunk {chunk_index} failed integrity check (tampered or corrupt)")


class ChunkMissingError(ChunkCryptError):
    """文件被截断，某一块数据缺失，携带块号。"""

    def __init__(self, chunk_index: int):
        self.chunk_index = chunk_index
        super().__init__(f"chunk {chunk_index} is missing (file truncated or chunk deleted)")


def generate_key() -> bytes:
    """生成 32 字节随机主密钥。"""
    return os.urandom(KEY_SIZE)


def _derive_keys(master_key: bytes) -> tuple[bytes, bytes]:
    enc_key = hmac.new(master_key, b"chunkcrypt/enc", hashlib.sha256).digest()
    mac_key = hmac.new(master_key, b"chunkcrypt/mac", hashlib.sha256).digest()
    return enc_key, mac_key


def _keystream(enc_key: bytes, nonce: bytes, chunk_index: int,
               block_offset: int, length: int) -> bytes:
    """生成块内从 block_offset 字节起、length 字节的密钥流。"""
    out = bytearray()
    counter = block_offset // MAC_SIZE
    skip = block_offset % MAC_SIZE
    prefix = enc_key + nonce + struct.pack("<Q", chunk_index)
    while len(out) < length + skip:
        out += hashlib.sha256(prefix + struct.pack("<Q", counter)).digest()
        counter += 1
    return bytes(out[skip:skip + length])


def _xor(data: bytes, stream: bytes) -> bytes:
    # 大整数异或，避免逐字节 Python 循环；长度 <= chunk_size。
    return (int.from_bytes(data, "little")
            ^ int.from_bytes(stream, "little")).to_bytes(len(data), "little")


def _chunk_mac(mac_key: bytes, chunk_index: int, ciphertext: bytes) -> bytes:
    return hmac.new(mac_key, b"chunk" + struct.pack("<Q", chunk_index) + ciphertext,
                    hashlib.sha256).digest()


def _resolve_key(key: bytes | None, password: str | bytes | None,
                 salt: bytes, iterations: int) -> bytes:
    if key is not None:
        if len(key) != KEY_SIZE:
            raise ChunkCryptError("key must be 32 bytes")
        return key
    if password is None:
        raise ChunkCryptError("either key or password is required")
    if isinstance(password, str):
        password = password.encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", password, salt, iterations,
                               dklen=KEY_SIZE)


def encrypt_stream(src: io.BufferedIOBase, dst: io.BufferedIOBase,
                   key: bytes | None = None, password: str | bytes | None = None,
                   chunk_size: int = DEFAULT_CHUNK_SIZE,
                   kdf_iterations: int = DEFAULT_KDF_ITERATIONS,
                   total_size: int | None = None) -> dict:
    """把 src 的全部内容加密写入 dst，返回元数据 dict。

    写入是流式的：索引常驻内存（每块 32 字节），明文按块读入，
    因此可处理远超内存的文件。
    """
    if chunk_size <= 0:
        raise ChunkCryptError("chunk_size must be positive")
    if total_size is None:
        pos = src.tell()
        src.seek(0, os.SEEK_END)
        total_size = src.tell() - pos
        src.seek(pos)
    chunk_count = (total_size + chunk_size - 1) // chunk_size if total_size else 0

    salt = os.urandom(SALT_SIZE) if key is None else b"\x00" * SALT_SIZE
    iterations = kdf_iterations if key is None else 0
    master = _resolve_key(key, password, salt, iterations)
    enc_key, mac_key = _derive_keys(master)
    nonce = os.urandom(NONCE_SIZE)

    header = _HEADER_STRUCT.pack(MAGIC, chunk_size, total_size, chunk_count,
                                 iterations) + salt + nonce
    header_mac = hmac.new(mac_key, b"header" + header, hashlib.sha256).digest()

    index_size = chunk_count * MAC_SIZE
    dst.write(header + header_mac)
    dst.write(b"\x00" * (index_size + INDEX_MAC_SIZE))  # 预留索引区

    index = bytearray()
    data_offset = HEADER_SIZE + index_size + INDEX_MAC_SIZE
    remaining = total_size
    for i in range(chunk_count):
        plain = src.read(min(chunk_size, remaining))
        remaining -= len(plain)
        cipher = _xor(plain, _keystream(enc_key, nonce, i, 0, len(plain)))
        index += _chunk_mac(mac_key, i, cipher)
        dst.write(cipher)

    index_mac = hmac.new(mac_key, b"index" + bytes(index), hashlib.sha256).digest()
    dst.seek(HEADER_SIZE)  # 回填索引
    dst.write(bytes(index) + index_mac)
    dst.seek(0, os.SEEK_END)

    return {"chunk_size": chunk_size, "total_size": total_size,
            "chunk_count": chunk_count, "file_size": data_offset + total_size}


def encrypt_file(src_path: str, dst_path: str, **kwargs) -> dict:
    """加密文件，返回元数据 dict。"""
    with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
        return encrypt_stream(src, dst, **kwargs)


class ChunkReader:
    """加密文件的随机访问读取器。

    只读取并解密所请求区间覆盖到的块；每块返回前先做 HMAC 校验，
    篡改会抛出携带块号的 ChunkTamperedError。
    """

    def __init__(self, path: str, key: bytes | None = None,
                 password: str | bytes | None = None):
        self._f = open(path, "rb")
        try:
            self._load(key, password)
        except BaseException:
            self._f.close()
            raise

    def _load(self, key: bytes | None, password: str | bytes | None) -> None:
        raw = self._f.read(HEADER_SIZE)
        if len(raw) < HEADER_SIZE:
            raise ChunkCryptError("file too small to be a chunkcrypt file")
        (magic, self.chunk_size, self.total_size, self.chunk_count,
         iterations) = _HEADER_STRUCT.unpack(raw[:_HEADER_STRUCT.size])
        if magic != MAGIC:
            raise ChunkCryptError("bad magic: not a chunkcrypt file")
        salt = raw[_HEADER_STRUCT.size:_HEADER_STRUCT.size + SALT_SIZE]
        self._nonce = raw[_HEADER_STRUCT.size + SALT_SIZE:
                          _HEADER_STRUCT.size + SALT_SIZE + NONCE_SIZE]
        header_mac = raw[-MAC_SIZE:]

        master = _resolve_key(key, password, salt, iterations)
        self._enc_key, self._mac_key = _derive_keys(master)

        expect = hmac.new(self._mac_key, b"header" + raw[:-MAC_SIZE],
                          hashlib.sha256).digest()
        if not hmac.compare_digest(expect, header_mac):
            raise IntegrityError("header integrity check failed "
                                 "(wrong key/password or corrupted metadata)")

        index_size = self.chunk_count * MAC_SIZE
        index = self._f.read(index_size)
        index_mac = self._f.read(INDEX_MAC_SIZE)
        if len(index) != index_size or len(index_mac) != INDEX_MAC_SIZE:
            raise IntegrityError("index region truncated")
        expect = hmac.new(self._mac_key, b"index" + index,
                          hashlib.sha256).digest()
        if not hmac.compare_digest(expect, index_mac):
            raise IntegrityError("index integrity check failed "
                                 "(chunk table tampered or reordered)")
        self._index = index

        self._data_offset = HEADER_SIZE + index_size + INDEX_MAC_SIZE
        expected_file_size = self._data_offset + self.total_size
        actual_size = os.fstat(self._f.fileno()).st_size
        if actual_size < expected_file_size:
            missing = max(0, (actual_size - self._data_offset)) // self.chunk_size
            raise ChunkMissingError(missing)
        if actual_size > expected_file_size:
            raise IntegrityError("file has trailing garbage beyond expected size")

    def close(self) -> None:
        self._f.close()

    def __enter__(self) -> "ChunkReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _chunk_bounds(self, chunk_index: int) -> tuple[int, int]:
        start = chunk_index * self.chunk_size
        length = min(self.chunk_size, self.total_size - start)
        return start, length

    def _read_cipher_chunk(self, chunk_index: int) -> bytes:
        """读取一个块的密文并做 HMAC 校验（不生成密钥流）。"""
        start, length = self._chunk_bounds(chunk_index)
        self._f.seek(self._data_offset + start)
        cipher = self._f.read(length)
        if len(cipher) != length:
            raise ChunkMissingError(chunk_index)
        expect = self._index[chunk_index * MAC_SIZE:(chunk_index + 1) * MAC_SIZE]
        actual = _chunk_mac(self._mac_key, chunk_index, cipher)
        if not hmac.compare_digest(expect, actual):
            raise ChunkTamperedError(chunk_index)
        return cipher

    def _read_chunk(self, chunk_index: int) -> bytes:
        """读取、校验并完整解密一个块。"""
        cipher = self._read_cipher_chunk(chunk_index)
        _, length = self._chunk_bounds(chunk_index)
        return _xor(cipher, _keystream(self._enc_key, self._nonce,
                                       chunk_index, 0, length))

    def _read_chunk_slice(self, chunk_index: int, lo: int, hi: int) -> bytes:
        """读取同一块内 [lo, hi)：先整块校验，再只生成该切片的密钥流。"""
        cipher = self._read_cipher_chunk(chunk_index)
        sub = cipher[lo:hi]
        return _xor(sub, _keystream(self._enc_key, self._nonce,
                                    chunk_index, lo, len(sub)))

    def read_at(self, offset: int, length: int) -> bytes:
        """读取 [offset, offset+length) 明文；只解密覆盖到的块。"""
        if offset < 0 or length < 0 or offset + length > self.total_size:
            raise ChunkCryptError("read range out of bounds")
        if length == 0:
            return b""
        first = offset // self.chunk_size
        last = (offset + length - 1) // self.chunk_size
        parts = []
        for i in range(first, last + 1):
            chunk_start = i * self.chunk_size
            lo = max(offset, chunk_start) - chunk_start
            hi = min(offset + length, chunk_start + self.chunk_size,
                     self.total_size) - chunk_start
            parts.append(self._read_chunk_slice(i, lo, hi))
        return b"".join(parts)

    def read_all(self) -> bytes:
        return self.read_at(0, self.total_size)

    def decrypt_to(self, dst_path: str) -> None:
        with open(dst_path, "wb") as out:
            for i in range(self.chunk_count):
                out.write(self._read_chunk(i))


def decrypt_file(src_path: str, dst_path: str, **kwargs) -> None:
    """整份解密到 dst_path（内部仍逐块校验）。"""
    with ChunkReader(src_path, **kwargs) as reader:
        reader.decrypt_to(dst_path)
