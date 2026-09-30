"""chunkcrypt 自测：往返无损、逐块篡改定位、块删除、边界用例。

运行: python3 -m unittest -v test_chunkcrypt
大文件用例默认 64MiB，可通过环境变量 LARGE_BYTES 调整。
"""

import os
import random
import tempfile
import unittest

import chunkcrypt as cc


class RoundTripMixin:
    chunk_size = 4096

    def _case(self, data: bytes):
        key = cc.generate_key()
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        if True:
            plain = os.path.join(d, "plain.bin")
            enc = os.path.join(d, "enc.bin")
            dec = os.path.join(d, "dec.bin")
            with open(plain, "wb") as f:
                f.write(data)
            cc.encrypt_file(plain, enc, key=key, chunk_size=self.chunk_size)
            with cc.ChunkReader(enc, key=key) as r:
                got_all = r.read_all()
                self.assertEqual(got_all, data, "read_all mismatch")
            cc.decrypt_file(enc, dec, key=key)
            with open(dec, "rb") as f:
                self.assertEqual(f.read(), data, "decrypt_to mismatch")
            return enc, key

    def test_random_reads(self):
        data = random.randbytes(self.chunk_size * 4 + 123)
        key = cc.generate_key()
        with tempfile.TemporaryDirectory() as d:
            plain = os.path.join(d, "p")
            enc = os.path.join(d, "e")
            with open(plain, "wb") as f:
                f.write(data)
            cc.encrypt_file(plain, enc, key=key, chunk_size=self.chunk_size)
            with cc.ChunkReader(enc, key=key) as r:
                self.assertEqual(r.read_at(0, 0), b"")
                for _ in range(200):
                    start = random.randint(0, len(data) - 1)
                    length = random.randint(1, min(20000, len(data) - start))
                    self.assertEqual(r.read_at(start, length),
                                     data[start:start + length],
                                     f"random read {start}+{length}")
                # 显式覆盖块边界：两端不对齐、中间跨多块
                s = self.chunk_size - 7
                self.assertEqual(r.read_at(s, 21), data[s:s + 21])
                s = 2 * self.chunk_size - 100
                self.assertEqual(r.read_at(s, 2 * self.chunk_size + 200),
                                 data[s:s + 2 * self.chunk_size + 200])


class TestEmpty(unittest.TestCase, RoundTripMixin):
    def test_empty(self):
        enc, key = self._case(b"")
        with cc.ChunkReader(enc, key=key) as r:
            self.assertEqual(r.chunk_count, 0)
            self.assertEqual(r.read_all(), b"")
        # 空文件加密产物只有头部 + 空索引 mac
        self.assertEqual(os.path.getsize(enc),
                         cc.HEADER_SIZE + cc.INDEX_MAC_SIZE)


class TestSingleChunk(unittest.TestCase, RoundTripMixin):
    def test_single_chunk_exact(self):
        self._case(bytes(range(256)) * 16)  # 4096 == chunk_size

    def test_single_chunk_partial(self):
        self._case(b"hello chunkcrypt" * 10)


class TestMultiChunk(unittest.TestCase, RoundTripMixin):
    chunk_size = 1000  # 故意不对齐 32B 密钥流块

    def test_multi_chunk_with_tail(self):
        self._case(random.randbytes(3 * 1000 + 7))

    def test_multi_chunk_exact(self):
        self._case(random.randbytes(3 * 1000))


class TestPassword(unittest.TestCase):
    def test_password_roundtrip_and_wrong_password(self):
        data = b"password-based encryption test" * 50
        with tempfile.TemporaryDirectory() as d:
            p, e = os.path.join(d, "p"), os.path.join(d, "e")
            with open(p, "wb") as f:
                f.write(data)
            cc.encrypt_file(p, e, password="correct horse battery staple",
                            chunk_size=1024, kdf_iterations=1000)
            with cc.ChunkReader(e, password="correct horse battery staple") as r:
                self.assertEqual(r.read_all(), data)
            with self.assertRaises(cc.IntegrityError):
                cc.ChunkReader(e, password="wrong password")


class TestTamper(unittest.TestCase, RoundTripMixin):
    chunk_size = 4096

    def test_chunk_tamper_localized(self):
        data = random.randbytes(5 * 4096 + 50)
        enc, key = self._case(data)
        target = 2
        pos = cc.HEADER_SIZE + cc.INDEX_MAC_SIZE + 5 * cc.MAC_SIZE + \
            target * self.chunk_size + 1234
        with open(enc, "r+b") as f:
            f.seek(pos)
            f.write(bytes([f.read(1)[0] ^ 0xFF]))
        with cc.ChunkReader(enc, key=key) as r:
            with self.assertRaises(cc.ChunkTamperedError) as ctx:
                r._read_chunk(target)
            self.assertEqual(ctx.exception.chunk_index, target)
            # 未被篡改的块仍可正常读取（不连带拒绝）
            self.assertEqual(r._read_chunk(0),
                             data[0:4096])
            # 读取覆盖被篡改块的区间同样被拒，且定位到块号
            with self.assertRaises(cc.ChunkTamperedError) as ctx2:
                r.read_at(self.chunk_size * 2 - 1, 3)
            self.assertEqual(ctx2.exception.chunk_index, target)

    def test_last_chunk_tamper_localized(self):
        data = random.randbytes(2 * 4096 + 37)
        enc, key = self._case(data)
        with open(enc, "r+b") as f:
            f.seek(-1, os.SEEK_END)
            b = f.read(1)[0]
            f.seek(-1, os.SEEK_END)
            f.write(bytes([b ^ 0x01]))
        with cc.ChunkReader(enc, key=key) as r:
            with self.assertRaises(cc.ChunkTamperedError) as ctx:
                r._read_chunk(2)
            self.assertEqual(ctx.exception.chunk_index, 2)

    def test_chunk_deleted(self):
        data = random.randbytes(4 * 4096)
        enc, key = self._case(data)
        # 删掉文件中最后一块的数据（保留头部+索引）
        with open(enc, "r+b") as f:
            f.truncate(os.path.getsize(enc) - 4096)
        with self.assertRaises(cc.ChunkMissingError) as ctx:
            cc.ChunkReader(enc, key=key)
        self.assertEqual(ctx.exception.chunk_index, 3)

    def test_chunk_deleted_middle(self):
        # 中间删块在定长格式下表现为文件截断：检测到 3 号块起缺失
        data = random.randbytes(4 * 4096)
        enc, key = self._case(data)
        with open(enc, "r+b") as f:
            f.truncate(os.path.getsize(enc) - 2 * 4096)
        with self.assertRaises(cc.ChunkMissingError) as ctx:
            cc.ChunkReader(enc, key=key)
        self.assertEqual(ctx.exception.chunk_index, 2)

    def test_middle_chunk_deleted_size_preserved(self):
        # 从数据区中间挖掉一整块并在尾部补零保持文件长度：
        # 数据整体错位，块 2 的 HMAC 必须失配并定位到 2。
        data = random.randbytes(5 * 4096)
        enc, key = self._case(data)
        with open(enc, "r+b") as f:
            cut = cc.HEADER_SIZE + cc.INDEX_MAC_SIZE + 5 * cc.MAC_SIZE +                 2 * self.chunk_size
            blob = f.read()
            f.seek(cut)
            f.write(blob[cut + self.chunk_size:])
            f.seek(-self.chunk_size, os.SEEK_END)
            f.write(b"\x00" * self.chunk_size)
        with cc.ChunkReader(enc, key=key) as r:
            with self.assertRaises(cc.ChunkTamperedError) as ctx:
                r._read_chunk(2)
            self.assertEqual(ctx.exception.chunk_index, 2)
            with self.assertRaises(cc.ChunkTamperedError):
                r.read_all()

    def test_index_tamper_detected(self):
        data = b"x" * (2 * 4096)
        enc, key = self._case(data)
        with open(enc, "r+b") as f:
            f.seek(cc.HEADER_SIZE)  # 索引区起点
            f.write(b"\xff" * 4)
        with self.assertRaises(cc.IntegrityError):
            cc.ChunkReader(enc, key=key)

    def test_header_tamper_detected(self):
        data = b"x" * 4096
        enc, key = self._case(data)
        with open(enc, "r+b") as f:
            f.seek(9)  # chunk_size 字段
            f.write(b"\x00")
        with self.assertRaises(cc.ChunkCryptError):
            cc.ChunkReader(enc, key=key)

    def test_out_of_bounds(self):
        data = b"a" * 5000
        enc, key = self._case(data)
        with cc.ChunkReader(enc, key=key) as r:
            with self.assertRaises(cc.ChunkCryptError):
                r.read_at(4999, 2)


class TestLargeFile(unittest.TestCase):
    """较大文件端到端往返（默认 64MiB）。耗时数据由 bench.py 生成。"""

    size = int(os.environ.get("LARGE_BYTES", 64 * 1024 * 1024))
    chunk_size = 1 << 20

    def test_large_roundtrip_and_random_read(self):
        import hashlib
        key = cc.generate_key()
        with tempfile.TemporaryDirectory() as d:
            p, e = os.path.join(d, "p"), os.path.join(d, "e")
            h = hashlib.sha256()
            with open(p, "wb") as f:
                block = random.randbytes(1 << 20)
                written = 0
                while written < self.size:
                    b = block[:min(1 << 20, self.size - written)]
                    f.write(b)
                    h.update(b)
                    written += len(b)
            digest = h.digest()

            meta = cc.encrypt_file(p, e, key=key, chunk_size=self.chunk_size)
            self.assertEqual(meta["chunk_count"],
                             (self.size + self.chunk_size - 1) // self.chunk_size)

            with cc.ChunkReader(e, key=key) as r:
                h2 = hashlib.sha256()
                for i in range(r.chunk_count):
                    h2.update(r._read_chunk(i))
                self.assertEqual(h2.digest(), digest)
                # 随机片段读取也必须一致
                with open(p, "rb") as f:
                    start = self.size // 2 - 123
                    f.seek(start)
                    want = f.read(5_000_000)
                self.assertEqual(r.read_at(start, len(want)), want)


if __name__ == "__main__":
    unittest.main(verbosity=2)
