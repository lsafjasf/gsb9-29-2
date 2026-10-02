"""
chunkenc.py - 分块认证加密文件格式（仅依赖 Python 3 标准库）。

为什么不用 AES：Python 标准库不包含 AES/ChaCha20 等对称密码。
本实现用标准库内的 HMAC-SHA256 作为 PRF，以计数器模式派生密钥流：

    keystream_block(j) = HMAC-SHA256(enc_key,
                                     nonce || chunk_index(u64be) || j(u64be))

每个 HMAC 输出 32 字节，拼接后与明文异或。HMAC-SHA256 是标准 PRF，
计数器模式用法在 PRF 安全时可证明安全；nonce 每块随机 16 字节。

完整性采用 Encrypt-then-MAC，逐块 HMAC-SHA256，MAC 绑定：
    "CHNK" || 块号 || nonce || 块在文件内偏移 || 密文长度 || 密文
因此单字节篡改、块替换/重排都会让对应块号的 MAC 校验失败。

容器布局（大端序）：

    偏移 0            文件头 header（含整头 MAC）
    偏移 HEADER_LEN   密文块 0, 1, ..., n-1（长度 = 明文块长度，无填充）
    偏移 index_offset 块索引（魔数 + 块数 + 每项[偏移,长度,nonce,MAC] + 整索引 MAC）
    文件末尾 16 字节  尾部：index_offset(u64be) || FOOTER_MAGIC

随机读 [start, end) 只访问 start//chunk_size .. (end-1)//chunk_size 这些块，
每块整体读入并校验 MAC（块是最小认证单位），再切片返回所需区间。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
from typing import BinaryIO, Optional, Union

MAGIC = b"CHNCENC1"
INDEX_MAGIC = b"CHNCIDX1"
FOOTER_MAGIC = b"CHNCEND1"

VERSION = 1
DEFAULT_CHUNK_SIZE = 1024 * 1024  # 1 MiB 明文 / 块
DEFAULT_ITERATIONS = 200_000
SALT_LEN = 16
NONCE_LEN = 16
MAC_LEN = 32

# magic(8) + version(B) + chunk_size(I) + plaintext_len(Q) + salt(16s) + iterations(I)
HEADER_BODY_LEN = 8 + 1 + 4 + 8 + 16 + 4
HEADER_LEN = HEADER_BODY_LEN + MAC_LEN
FOOTER_LEN = 8 + 8
ENTRY_LEN = 8 + 8 + NONCE_LEN + MAC_LEN  # offset, length, nonce, mac

_KEY_ENC_LABEL = b"chunkenc-enc-v1"
_KEY_MAC_LABEL = b"chunkenc-mac-v1"


class CorruptContainerError(Exception):
    """容器结构、文件头或块索引不可信。"""


class ChunkIntegrityError(CorruptContainerError):
    """某一块密文或其索引项校验失败，chunk_index 即被定位到的块号。"""

    def __init__(self, chunk_index: int, message: Optional[str] = None):
        self.chunk_index = chunk_index
        if message is None:
            message = "integrity check failed at chunk %d" % chunk_index
        super().__init__(message)


def _derive_keys(password: Union[str, bytes], salt: bytes,
                 iterations: int) -> tuple[bytes, bytes]:
    if isinstance(password, str):
        password = password.encode("utf-8")
    material = hashlib.pbkdf2_hmac("sha256", password, salt, iterations, dklen=64)
    # 域分离，避免加密密钥与 MAC 密钥简单同源。
    enc_key = hashlib.pbkdf2_hmac("sha256", material, _KEY_ENC_LABEL, 1, dklen=32)
    mac_key = hashlib.pbkdf2_hmac("sha256", material, _KEY_MAC_LABEL, 1, dklen=32)
    return enc_key, mac_key


def _mac(mac_key: bytes, *parts: bytes) -> bytes:
    m = hmac.new(mac_key, digestmod=hashlib.sha256)
    for part in parts:
        m.update(part)
    return m.digest()


def _keystream_xor(enc_key: bytes, chunk_index: int, nonce: bytes,
                   data: bytes) -> bytes:
    """HMAC-SHA256 计数器模式密钥流，与 data 异或。加解密同一函数。"""
    if not data:
        return b""
    blocks = []
    needed = len(data)
    counter = 0
    prefix = nonce + struct.pack(">Q", chunk_index)
    while needed > 0:
        block = hmac.new(enc_key, prefix + struct.pack(">Q", counter),
                         hashlib.sha256).digest()
        blocks.append(block)
        needed -= len(block)
        counter += 1
    keystream = b"".join(blocks)[:len(data)]
    # 大整数异或是 C 级实现，比逐字节循环快得多。
    a = int.from_bytes(data, "big")
    b = int.from_bytes(keystream, "big")
    return (a ^ b).to_bytes(len(data), "big")


def _chunk_mac(mac_key: bytes, index: int, nonce: bytes,
               offset: int, ciphertext: bytes) -> bytes:
    return _mac(mac_key, b"CHNK", struct.pack(">Q", index), nonce,
                struct.pack(">QQ", offset, len(ciphertext)), ciphertext)


def _stream_len(fin: BinaryIO) -> int:
    pos = fin.tell()
    fin.seek(0, os.SEEK_END)
    end = fin.tell()
    fin.seek(pos)
    return end


def encrypt_file(src: Union[str, os.PathLike, BinaryIO],
                 dst: Union[str, os.PathLike, BinaryIO],
                 password: Union[str, bytes],
                 chunk_size: int = DEFAULT_CHUNK_SIZE,
                 iterations: int = DEFAULT_ITERATIONS,
                 salt: Optional[bytes] = None) -> None:
    """把 src 全量加密为容器 dst（流式处理，内存中只保留一个块）。"""
    if chunk_size <= 0 or chunk_size > 0xFFFFFFFF:
        raise ValueError("chunk_size 必须在 1..2^32-1 之间")
    if salt is None:
        salt = os.urandom(SALT_LEN)
    if len(salt) != SALT_LEN:
        raise ValueError("salt 长度必须为 %d" % SALT_LEN)

    owns_src = isinstance(src, (str, os.PathLike))
    owns_dst = isinstance(dst, (str, os.PathLike))
    fin = open(src, "rb") if owns_src else src
    fout = open(dst, "wb") if owns_dst else dst
    try:
        enc_key, mac_key = _derive_keys(password, salt, iterations)

        plaintext_len = _stream_len(fin)
        header_body = (
            MAGIC
            + struct.pack(">B", VERSION)
            + struct.pack(">I", chunk_size)
            + struct.pack(">Q", plaintext_len)
            + salt
            + struct.pack(">I", iterations)
        )
        fout.write(header_body)
        fout.write(_mac(mac_key, b"HDR", header_body))

        entries: list[tuple[int, int, bytes, bytes]] = []
        index = 0
        while True:
            plaintext = fin.read(chunk_size)
            if not plaintext:
                break
            nonce = os.urandom(NONCE_LEN)
            ciphertext = _keystream_xor(enc_key, index, nonce, plaintext)
            offset = fout.tell()
            fout.write(ciphertext)
            tag = _chunk_mac(mac_key, index, nonce, offset, ciphertext)
            entries.append((offset, len(ciphertext), nonce, tag))
            index += 1

        index_offset = fout.tell()
        index_head = INDEX_MAGIC + struct.pack(">Q", len(entries))
        fout.write(index_head)
        for offset, length, nonce, tag in entries:
            fout.write(struct.pack(">QQ", offset, length))
            fout.write(nonce)
            fout.write(tag)
        fout.write(_mac(mac_key, b"IDX", index_head, *[
            struct.pack(">QQ", off, ln) + nonce + tag
            for off, ln, nonce, tag in entries
        ]))
        fout.write(struct.pack(">Q", index_offset))
        fout.write(FOOTER_MAGIC)
    finally:
        if owns_src:
            fin.close()
        if owns_dst:
            fout.close()


class ChunkReader:
    """对已加密容器的随机访问只读视图，接口类似文件对象。"""

    def __init__(self, path_or_file: Union[str, os.PathLike, BinaryIO],
                 password: Union[str, bytes]):
        owns = isinstance(path_or_file, (str, os.PathLike))
        self._f = open(path_or_file, "rb") if owns else path_or_file
        self._owns = owns
        try:
            self._open_container(password)
        except Exception:
            if owns:
                self._f.close()
            raise
        self._pos = 0

    # ---- 容器解析 ----

    def _open_container(self, password: Union[str, bytes]) -> None:
        f = self._f
        f.seek(0, os.SEEK_END)
        file_len = f.tell()
        if file_len < HEADER_LEN + FOOTER_LEN + len(INDEX_MAGIC) + 8 + MAC_LEN:
            raise CorruptContainerError("文件太小，不是有效容器")

        f.seek(0)
        header_body = f.read(HEADER_BODY_LEN)
        header_mac = f.read(MAC_LEN)
        if len(header_body) != HEADER_BODY_LEN or len(header_mac) != MAC_LEN:
            raise CorruptContainerError("文件头不完整")
        if header_body[:8] != MAGIC:
            raise CorruptContainerError("魔数不匹配，不是本格式容器")
        version = header_body[8]
        if version != VERSION:
            raise CorruptContainerError("不支持的格式版本 %d" % version)
        self.chunk_size = struct.unpack(">I", header_body[9:13])[0]
        self.plaintext_len = struct.unpack(">Q", header_body[13:21])[0]
        salt = header_body[21:37]
        iterations = struct.unpack(">I", header_body[37:41])[0]

        self._enc_key, self._mac_key = _derive_keys(password, salt, iterations)
        if not hmac.compare_digest(header_mac,
                                   _mac(self._mac_key, b"HDR", header_body)):
            raise CorruptContainerError("文件头校验失败（口令错误或头部被篡改）")

        f.seek(file_len - FOOTER_LEN)
        footer = f.read(FOOTER_LEN)
        if len(footer) != FOOTER_LEN or footer[8:] != FOOTER_MAGIC:
            raise CorruptContainerError("尾部缺失或被破坏")
        index_offset = struct.unpack(">Q", footer[:8])[0]
        if not (HEADER_LEN <= index_offset <= file_len - FOOTER_LEN):
            raise CorruptContainerError("尾部索引偏移越界")

        f.seek(index_offset)
        index_head = f.read(len(INDEX_MAGIC) + 8)
        if len(index_head) != len(INDEX_MAGIC) + 8 \
                or index_head[:len(INDEX_MAGIC)] != INDEX_MAGIC:
            raise CorruptContainerError("块索引魔数不匹配")
        n_chunks = struct.unpack(">Q", index_head[8:])[0]

        expected_chunks = (self.plaintext_len + self.chunk_size - 1) \
            // self.chunk_size
        if n_chunks != expected_chunks:
            raise CorruptContainerError(
                "块数 %d 与文件头声明的明文长度不符（应有 %d 块）"
                % (n_chunks, expected_chunks))

        entries_raw = f.read(n_chunks * ENTRY_LEN)
        if len(entries_raw) != n_chunks * ENTRY_LEN:
            raise CorruptContainerError("块索引被截断")
        index_mac = f.read(MAC_LEN)
        if len(index_mac) != MAC_LEN:
            raise CorruptContainerError("块索引 MAC 缺失")
        if not hmac.compare_digest(
                index_mac, _mac(self._mac_key, b"IDX", index_head, entries_raw)):
            raise CorruptContainerError("块索引校验失败（索引被篡改）")

        self._entries: list[tuple[int, int, bytes, bytes]] = []
        prev_end = HEADER_LEN
        for i in range(n_chunks):
            base = i * ENTRY_LEN
            offset, length = struct.unpack(
                ">QQ", entries_raw[base:base + 16])
            nonce = entries_raw[base + 16:base + 16 + NONCE_LEN]
            tag = entries_raw[base + 16 + NONCE_LEN:base + ENTRY_LEN]
            # 结构一致性：块必须按序无缝衔接，且总长度等于明文长度。
            if offset != prev_end:
                raise CorruptContainerError("块 %d 偏移不连续" % i)
            if i < n_chunks - 1 and length != self.chunk_size:
                raise CorruptContainerError("中间块 %d 长度异常" % i)
            if i == n_chunks - 1 and \
                    length != self.plaintext_len - self.chunk_size * (n_chunks - 1):
                raise CorruptContainerError("末块长度与明文长度不符")
            if offset + length > index_offset:
                raise CorruptContainerError("块 %d 越过索引区" % i)
            prev_end = offset + length
            self._entries.append((offset, length, nonce, tag))

        self.n_chunks = n_chunks

    # ---- 块级读取 ----

    def read_chunk(self, index: int) -> bytes:
        """读取并校验第 index 块，返回明文。失败抛 ChunkIntegrityError。"""
        if not (0 <= index < self.n_chunks):
            raise IndexError("chunk index %d out of range" % index)
        offset, length, nonce, tag = self._entries[index]
        self._f.seek(offset)
        ciphertext = self._f.read(length)
        if len(ciphertext) != length:
            raise ChunkIntegrityError(index, "块 %d 密文被截断" % index)
        actual = _chunk_mac(self._mac_key, index, nonce, offset, ciphertext)
        if not hmac.compare_digest(actual, tag):
            raise ChunkIntegrityError(index)
        return _keystream_xor(self._enc_key, index, nonce, ciphertext)

    # ---- 文件式接口 ----

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            new = offset
        elif whence == os.SEEK_CUR:
            new = self._pos + offset
        elif whence == os.SEEK_END:
            new = self.plaintext_len + offset
        else:
            raise ValueError("invalid whence")
        if new < 0:
            raise ValueError("negative seek position")
        self._pos = new
        return self._pos

    def tell(self) -> int:
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if self.plaintext_len == 0 or self._pos >= self.plaintext_len:
            return b""
        if size is None or size < 0:
            size = self.plaintext_len - self._pos
        end = min(self._pos + size, self.plaintext_len)
        if end <= self._pos:
            return b""
        # 只解密覆盖 [pos, end) 的块：first..last（含两端）。
        first = self._pos // self.chunk_size
        last = (end - 1) // self.chunk_size
        parts = []
        for i in range(first, last + 1):
            chunk = self.read_chunk(i)
            lo = self._pos - i * self.chunk_size if i == first else 0
            hi = end - i * self.chunk_size if i == last else self.chunk_size
            parts.append(chunk[lo:hi])
        self._pos = end
        return b"".join(parts)

    def read_at(self, offset: int, size: int) -> bytes:
        """从明文偏移 offset 读取 size 字节（不改当前位置）。"""
        if offset < 0 or size < 0:
            raise ValueError("offset/size 不能为负")
        saved = self._pos
        try:
            self.seek(offset)
            return self.read(size)
        finally:
            self._pos = saved

    def verify_all(self) -> None:
        """逐块校验整个文件，第一处损坏即抛 ChunkIntegrityError。"""
        for i in range(self.n_chunks):
            self.read_chunk(i)

    def close(self) -> None:
        if self._owns:
            self._f.close()

    def __enter__(self) -> "ChunkReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def decrypt_file(src: Union[str, os.PathLike, BinaryIO],
                 dst: Union[str, os.PathLike, BinaryIO],
                 password: Union[str, bytes]) -> None:
    """整份解密（校验全部块）。随机读取请用 ChunkReader。"""
    owns_dst = isinstance(dst, (str, os.PathLike))
    fout = open(dst, "wb") if owns_dst else dst
    try:
        with ChunkReader(src, password) as reader:
            for i in range(reader.n_chunks):
                fout.write(reader.read_chunk(i))
    finally:
        if owns_dst:
            fout.close()
