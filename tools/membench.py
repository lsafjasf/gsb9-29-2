"""内存峰值测量：python3 tools/membench.py

对若干尺寸/管线组合，用 tracemalloc 测量 Python 堆峰值，
并验证缓冲池在重复运行时不再新增分配。
"""

import os
import random
import struct
import sys
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imgpipe import Pipeline  # noqa: E402
from imgpipe.buffer import MAGIC  # noqa: E402
from imgpipe.codec import dump_header  # noqa: E402


def make_blob(rng, w, h, ch):
    header = {"width": w, "height": h, "channels": ch, "meta": {}}
    hb = dump_header(header)
    return MAGIC + struct.pack("<I", len(hb)) + hb + rng.randbytes(w * h * ch)


def bench(label, w, h, ch, ops):
    rng = random.Random(99)
    blob = make_blob(rng, w, h, ch)
    raw = w * h * ch

    pipe = Pipeline()
    tracemalloc.start()
    pipe.run(blob, ops)
    _, peak_cold = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    allocs_cold = pipe.pool.allocations

    # 热态：缓冲池已建好，再跑 3 次看增量分配
    tracemalloc.start()
    pipe.run(blob, ops)
    _, peak_warm = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    mib = 1024 * 1024
    print("%-22s raw=%7.1f MiB | 冷启动峰值 %7.1f MiB (%4.2fx) | "
          "热态峰值 %7.1f MiB (%4.2fx) | 池: 冷分配 %d 次, 热新增 %d 次, 复用 %d 次"
          % (label, raw / mib, peak_cold / mib, peak_cold / raw,
             peak_warm / mib, peak_warm / raw,
             allocs_cold, pipe.pool.allocations - allocs_cold,
             pipe.pool.reuses))


def main():
    full_chain = {"grayscale": True, "brightness": 15, "invert": True,
                  "flip_h": True, "flip_v": True, "rotate90": True}
    print("%-22s %-13s | %-25s | %-25s | %s"
          % ("场景", "单缓冲", "冷启动(含池建立)", "热态(池复用)", "缓冲池"))
    bench("512x512 全链路", 512, 512, 3, full_chain)
    bench("512x512 模糊r=1", 512, 512, 3, {"blur": 1})
    bench("2048x1536 翻转", 2048, 1536, 3, {"flip_v": True})
    bench("4096x3072 翻转", 4096, 3072, 3, {"flip_v": True})


if __name__ == "__main__":
    main()
