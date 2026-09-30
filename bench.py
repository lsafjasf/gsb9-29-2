"""chunkcrypt 读写耗时基准（默认 256MiB，块大小 1MiB）。

运行: python3 bench.py [总字节数]
"""

import os
import random
import tempfile
import time

import chunkcrypt as cc


def main(total: int = 256 * 1024 * 1024, chunk_size: int = 1 << 20) -> None:
    import sys
    if len(sys.argv) > 1:
        total = int(sys.argv[1])
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "plain.bin")
        e = os.path.join(d, "enc.bin")
        o = os.path.join(d, "dec.bin")
        key = cc.generate_key()

        t0 = time.perf_counter()
        with open(p, "wb") as f:
            written = 0
            rng = random.Random(1234)
            while written < total:
                b = rng.randbytes(min(chunk_size, total - written))
                f.write(b)
                written += len(b)
        gen = time.perf_counter() - t0

        t0 = time.perf_counter()
        meta = cc.encrypt_file(p, e, key=key, chunk_size=chunk_size)
        enc_t = time.perf_counter() - t0

        t0 = time.perf_counter()
        with cc.ChunkReader(e, key=key) as r:
            r.decrypt_to(o)
        dec_t = time.perf_counter() - t0

        with open(p, "rb") as a, open(o, "rb") as b:
            assert a.read() == b.read(), "roundtrip mismatch!"

        timings = []
        with cc.ChunkReader(e, key=key) as r:
            for label, off, length in (
                ("块内 4KiB", 123456, 4096),
                ("跨块 4KiB", chunk_size - 2048, 4096),
                ("跨 3 块 2.5MiB", chunk_size - 1024, 2 * chunk_size + 1024),
                ("整块 1MiB", chunk_size, chunk_size),
            ):
                with open(p, "rb") as f:
                    f.seek(off)
                    want = f.read(length)
                t0 = time.perf_counter()
                for _ in range(20):
                    got = r.read_at(off, length)
                dt = (time.perf_counter() - t0) / 20
                assert got == want
                timings.append((label, length, dt))

        mb = total / 1024 / 1024
        print(f"文件大小: {mb:.1f} MiB, 块大小: {chunk_size // 1024} KiB, "
              f"块数: {meta['chunk_count']}")
        print(f"生成测试数据 : {gen:6.3f} s")
        print(f"加密(写)    : {enc_t:6.3f} s   {mb / enc_t:7.1f} MiB/s")
        print(f"解密(读全量) : {dec_t:6.3f} s   {mb / dec_t:7.1f} MiB/s")
        print("随机读取(含 HMAC 校验，20 次平均):")
        for label, length, dt in timings:
            print(f"  {label:<14} {length / 1024:7.1f} KiB -> {dt * 1000:8.3f} ms")


if __name__ == "__main__":
    main()
