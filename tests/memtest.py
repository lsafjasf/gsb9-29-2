"""内存峰值测量：大图（默认 4000x3000，12MP）下用 tracemalloc 测管线峰值。

验证内存模型：峰值 ~= 输入压缩字节 C + 2 帧 F + 输出字节 O，与阶段数无关。
运行：python3 tests/memtest.py [宽 高]
"""
import gc
import os
import resource
import sys
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import BufferPool, ImageBuffer, build_pipeline
from pipeline.stages.encode import EncodeStage


def mib(n):
    return n / 1024.0 / 1024.0


def measure(label, bmp, config, frame):
    gc.collect()
    pool = BufferPool()
    tracemalloc.start()
    out, meta = build_pipeline(config, pool=pool).run(bmp)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    bound = len(bmp) + 2 * frame + len(out)
    print("%-34s 输入 %6.1f MiB  输出 %6.1f MiB  池峰值 %6.1f MiB"
          "  进程峰值(tracemalloc) %6.1f MiB  上界(C+2F+O) %6.1f MiB  maxRSS %6.1f MiB"
          % (label, mib(len(bmp)), mib(len(out)), mib(pool.max_live_bytes),
             mib(peak), mib(bound), mib(rss)))
    assert pool.max_live_bytes <= 2 * frame, "缓冲池在途超过 2 帧!"
    assert peak <= bound * 1.05, "进程峰值超出理论上界!"
    return peak


def main():
    w = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
    h = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
    frame = w * h * 3
    print("图像 %dx%d，单帧 F = %.1f MiB" % (w, h, mib(frame)))

    buf = ImageBuffer(bytearray(os.urandom(frame)), w, h, 3, {"ops": []})
    bmp, _ = EncodeStage().run(buf, BufferPool())
    del buf
    gc.collect()

    measure("decode+encode（直通）", bmp, {}, frame)
    measure("色彩 x2 + 几何 x2", bmp,
            {"color": [{"name": "grayscale"}, {"name": "invert"}],
             "geometry": [{"name": "flip_h"}, {"name": "rotate90"}]}, frame)
    measure("色彩 x6（阶段数增加）", bmp,
            {"color": [{"name": "invert"}, {"name": "sepia"}] * 3}, frame)
    measure("全阶段（含滤波）", bmp,
            {"color": [{"name": "grayscale"}],
             "geometry": [{"name": "rotate90"}],
             "filter": [{"name": "boxblur"}]}, frame)
    print("\n结论：在途帧缓冲恒定 <= 2F，进程峰值稳定在 C+2F+O 上界内，与阶段数无关。")


if __name__ == "__main__":
    main()
