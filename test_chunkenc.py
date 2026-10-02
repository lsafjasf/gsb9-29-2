"""chunkenc 自测：往返无损、随机读、逐块篡改定位、边界用例、耗时基准。"""

import os
import random
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chunkenc
from chunkenc import (
    ChunkIntegrityError, ChunkReader, CorruptContainerError,
    decrypt_file, encrypt_file, HEADER_LEN, FOOTER_LEN,
)

PASSWORD = "correct horse battery staple"
FAST = dict(iterations=1_000)  # 测试用低迭代，基准另测


def make_plain(path, size, seed=42):
    rng = random.Random(seed)
    chunk = bytes(rng.getrandbits(8) for _ in range(65536))
    with open(path, "wb") as f:
        remaining = size
        while remaining > 0:
            piece = chunk[:remaining]
            f.write(piece)
            remaining -= len(piece)


class TempDirMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plain = os.path.join(self.tmp.name, "plain.bin")
        self.enc = os.path.join(self.tmp.name, "cipher.cenc")
        self.dec = os.path.join(self.tmp.name, "dec.bin")


class RoundTripTest(TempDirMixin):
    def roundtrip(self, size, chunk_size):
        make_plain(self.plain, size)
        encrypt_file(self.plain, self.enc, PASSWORD,
                     chunk_size=chunk_size, **FAST)
        decrypt_file(self.enc, self.dec, PASSWORD)
        with open(self.plain, "rb") as f:
            original = f.read()
        with open(self.dec, "rb") as f:
            restored = f.read()
        self.assertEqual(original, restored)

    def test_empty_file(self):
        self.roundtrip(0, 4096)

    def test_one_byte(self):
        self.roundtrip(1, 4096)

    def test_single_chunk_exact(self):
        self.roundtrip(4096, 4096)

    def test_single_chunk_partial(self):
        self.roundtrip(4095, 4096)

    def test_multi_chunk_exact_boundary(self):
        self.roundtrip(4096 * 4, 4096)

    def test_multi_chunk_partial_tail(self):
        self.roundtrip(4096 * 4 + 1, 4096)

    def test_large_file(self):
        self.roundtrip(20 * 1024 * 1024 + 123, 1024 * 1024)

    def test_wrong_password_rejected(self):
        make_plain(self.plain, 10000)
        encrypt_file(self.plain, self.enc, PASSWORD, chunk_size=4096, **FAST)
        with self.assertRaises(CorruptContainerError):
            ChunkReader(self.enc, "wrong password")


class RandomReadTest(TempDirMixin):
    SIZE = 3 * 1024 * 1024 + 777
    CHUNK = 256 * 1024

    def setUp(self):
        super().setUp()
        make_plain(self.plain, self.SIZE)
        encrypt_file(self.plain, self.enc, PASSWORD,
                     chunk_size=self.CHUNK, **FAST)
        with open(self.plain, "rb") as f:
            self.original = f.read()

    def test_cross_chunk_read(self):
        with ChunkReader(self.enc, PASSWORD) as r:
            # 跨 3 个块边界的读取
            start = self.CHUNK - 100
            data = r.read_at(start, 2 * self.CHUNK + 200)
            self.assertEqual(data, self.original[start:start + len(data)])

    def test_random_reads_match(self):
        rng = random.Random(7)
        with ChunkReader(self.enc, PASSWORD) as r:
            for _ in range(200):
                off = rng.randrange(0, self.SIZE)
                size = rng.randrange(0, min(self.SIZE - off, 3 * self.CHUNK) + 1)
                self.assertEqual(r.read_at(off, size),
                                 self.original[off:off + size])

    def test_read_past_eof_and_empty_range(self):
        with ChunkReader(self.enc, PASSWORD) as r:
            self.assertEqual(r.read_at(self.SIZE, 100), b"")
            self.assertEqual(r.read_at(self.SIZE - 10, 1000),
                             self.original[-10:])
            self.assertEqual(r.read_at(0, 0), b"")

    def test_sequential_file_like(self):
        with ChunkReader(self.enc, PASSWORD) as r:
            self.assertEqual(r.read(), self.original)
            r.seek(self.CHUNK + 5)
            self.assertEqual(r.read(10), self.original[self.CHUNK + 5:self.CHUNK + 15])
            r.seek(-7, os.SEEK_END)
            self.assertEqual(r.read(), self.original[-7:])

    def test_random_read_touches_only_needed_chunks(self):
        """验证随机读只解密涉及的块：篡改未涉及的块不影响读取。"""
        with open(self.enc, "r+b") as f:
            # 篡改最后一块的密文
            with ChunkReader(self.enc, PASSWORD) as r:
                off, ln, _, _ = r._entries[-1]
            f.seek(off)
            b = bytearray(f.read(1))
            b[0] ^= 0xFF
            f.seek(off)
            f.write(bytes(b))
        with ChunkReader(self.enc, PASSWORD) as r:
            # 读第一块范围：不接触被篡改的末块，应成功
            self.assertEqual(r.read_at(0, 100), self.original[:100])
            # 读末块范围：必须报末块块号
            with self.assertRaises(ChunkIntegrityError) as cm:
                r.read_at(self.SIZE - 1, 1)
            self.assertEqual(cm.exception.chunk_index, r.n_chunks - 1)


class TamperLocalizationTest(TempDirMixin):
    SIZE = 5 * 4096 + 100
    CHUNK = 4096

    def setUp(self):
        super().setUp()
        make_plain(self.plain, self.SIZE)
        encrypt_file(self.plain, self.enc, PASSWORD,
                     chunk_size=self.CHUNK, **FAST)

    def corrupt_byte(self, offset):
        with open(self.enc, "r+b") as f:
            f.seek(offset)
            b = bytearray(f.read(1))
            b[0] ^= 0x01
            f.seek(offset)
            f.write(bytes(b))

    def test_flip_each_chunk_located(self):
        """逐块翻转密文一字节，verify_all 必须报出对应块号。"""
        with ChunkReader(self.enc, PASSWORD) as r:
            offsets = [e[0] for e in r._entries]
            n = r.n_chunks
        for i in range(n):
            enc_copy = self.enc + ".copy"
            with open(self.enc, "rb") as fsrc, open(enc_copy, "wb") as fdst:
                fdst.write(fsrc.read())
            self.corrupt_byte_at(enc_copy, offsets[i] + 1)
            with ChunkReader(enc_copy, PASSWORD) as r:
                with self.assertRaises(ChunkIntegrityError) as cm:
                    r.verify_all()
                self.assertEqual(cm.exception.chunk_index, i)
            os.unlink(enc_copy)

    def corrupt_byte_at(self, path, offset):
        with open(path, "r+b") as f:
            f.seek(offset)
            b = bytearray(f.read(1))
            b[0] ^= 0x01
            f.seek(offset)
            f.write(bytes(b))

    def test_tamper_index_entry(self):
        with ChunkReader(self.enc, PASSWORD) as r:
            index_offset = r._entries[0][0]  # 第一块偏移在索引里；先找索引位置
        # 索引区在最后一个块之后、尾部之前
        file_len = os.path.getsize(self.enc)
        index_offset = file_len - FOOTER_LEN - (8 + 8 + 6 * 56 + 32)
        self.corrupt_byte_at(self.enc, index_offset + 20)
        with self.assertRaises(CorruptContainerError):
            ChunkReader(self.enc, PASSWORD)

    def test_tamper_header(self):
        self.corrupt_byte(20)  # plaintext_len 字段
        with self.assertRaises(CorruptContainerError):
            ChunkReader(self.enc, PASSWORD)

    def test_deleted_tail_chunk(self):
        """删掉最后一个块（连同索引/尾部一起截断）-> 容器损坏，拒绝打开。"""
        with ChunkReader(self.enc, PASSWORD) as r:
            last_off = r._entries[-1][0]
        with open(self.enc, "r+b") as f:
            f.truncate(last_off)
        with self.assertRaises(CorruptContainerError):
            ChunkReader(self.enc, PASSWORD)

    def test_deleted_middle_chunk(self):
        """删掉中间一块并把索引/尾部前移 -> 索引 MAC 校验失败，拒绝打开。"""
        with ChunkReader(self.enc, PASSWORD) as r:
            off1, ln1 = r._entries[1][0], r._entries[1][1]
        with open(self.enc, "r+b") as f:
            f.seek(off1 + ln1)
            rest = f.read()
            f.seek(off1)
            f.write(rest)
            f.truncate(off1 + len(rest))
        with self.assertRaises(CorruptContainerError):
            ChunkReader(self.enc, PASSWORD)

    def test_truncated_inside_chunk(self):
        """块中间被截断（文件变短但尾部结构破坏）-> 拒绝打开。"""
        file_len = os.path.getsize(self.enc)
        with open(self.enc, "r+b") as f:
            f.truncate(file_len - 1000)
        with self.assertRaises(CorruptContainerError):
            ChunkReader(self.enc, PASSWORD)

    def test_chunk_swap_detected(self):
        """交换两块密文 -> 两块都报完整性错误（MAC 绑定块号）。"""
        with ChunkReader(self.enc, PASSWORD) as r:
            off0, ln0 = r._entries[0][0], r._entries[0][1]
            off1, ln1 = r._entries[1][0], r._entries[1][1]
        with open(self.enc, "r+b") as f:
            f.seek(off0); c0 = f.read(ln0)
            f.seek(off1); c1 = f.read(ln1)
            f.seek(off0); f.write(c1)
            f.seek(off1); f.write(c0)
        with ChunkReader(self.enc, PASSWORD) as r:
            with self.assertRaises(ChunkIntegrityError) as cm:
                r.read_chunk(0)
            self.assertEqual(cm.exception.chunk_index, 0)
            with self.assertRaises(ChunkIntegrityError) as cm:
                r.read_chunk(1)
            self.assertEqual(cm.exception.chunk_index, 1)


class BenchmarkTest(TempDirMixin):
    """耗时数据：写（加密）、整读（解密+校验）、随机读。"""

    def test_benchmark(self):
        sizes = [1 * 1024 * 1024, 64 * 1024 * 1024]
        print("\n%-10s %-10s %12s %12s %12s %14s" %
              ("明文大小", "块大小", "加密(s)", "整读解密(s)", "随机读(ms)", "随机读放大"))
        for size in sizes:
            for chunk_size in ([1024 * 1024] if size < 8 * 1024 * 1024
                               else [256 * 1024, 1024 * 1024, 4 * 1024 * 1024]):
                make_plain(self.plain, size)
                t0 = time.perf_counter()
                encrypt_file(self.plain, self.enc, PASSWORD,
                             chunk_size=chunk_size, iterations=1)
                t_enc = time.perf_counter() - t0

                t0 = time.perf_counter()
                decrypt_file(self.enc, self.dec, PASSWORD)
                t_dec = time.perf_counter() - t0

                rng = random.Random(1)
                read_len = 4096
                with ChunkReader(self.enc, PASSWORD) as r:
                    positions = [rng.randrange(0, size - read_len)
                                 for _ in range(50)]
                    t0 = time.perf_counter()
                    for pos in positions:
                        r.read_at(pos, read_len)
                    t_rand = (time.perf_counter() - t0) / len(positions) * 1000
                amplification = chunk_size / read_len
                print("%-10s %-10s %12.3f %12.3f %12.2f %13.0fx" % (
                    "%dMiB" % (size // 1024 // 1024),
                    "%dKiB" % (chunk_size // 1024),
                    t_enc, t_dec, t_rand, amplification))
                os.unlink(self.enc)
                os.unlink(self.dec)


if __name__ == "__main__":
    unittest.main(verbosity=2)
