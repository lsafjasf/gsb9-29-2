"""差分对拍：随机生成大量用例，逐例比较 legacy 大函数与管线重构的输出。

比较内容：编码后的 BMP 字节（逐字节）+ 元数据字典，必须完全一致。
运行：python3 tests/difftest.py [随机种子]
"""
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from legacy import process_image
from pipeline import build_pipeline, BufferPool, ImageBuffer
from pipeline.stages.encode import EncodeStage


def make_bmp(width, height, rng):
    """用独立的生成逻辑造一张合法 24 位 BMP（管线 encode 仅用作输入源）。"""
    raw = bytearray(rng.randbytes(width * height * 3))
    buf = ImageBuffer(raw, width, height, 3, {"ops": []})
    bmp, _ = EncodeStage().run(buf, BufferPool())
    return bmp


COLOR_OPS = ["invert", "grayscale", "sepia"]
GEO_OPS = ["flip_h", "rotate90"]
FILTER_OPS = ["boxblur", "sharpen"]

# 固定边界尺寸：覆盖 1x1、奇数宽（BMP 行对齐）、<3x3（滤波无内部像素）、非方形
EDGE_SIZES = [
    (1, 1), (1, 5), (5, 1), (2, 2), (3, 3), (2, 3), (3, 2),
    (4, 3), (7, 2), (7, 7), (31, 17), (17, 31), (65, 1), (1, 65),
]


def random_ops(rng):
    color = [{"name": rng.choice(COLOR_OPS)} for _ in range(rng.randint(0, 3))]
    geo = [{"name": rng.choice(GEO_OPS)} for _ in range(rng.randint(0, 2))]
    flt = [{"name": rng.choice(FILTER_OPS)} for _ in range(rng.randint(0, 2))]
    return color, geo, flt


def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 20261002
    rng = random.Random(seed)
    cases = []
    for w, h in EDGE_SIZES:
        cases.append((w, h, random_ops(rng)))
    cases.append((16, 16, ([], [], [])))  # 全部阶段跳过（直通）
    for _ in range(40):
        w = rng.randint(1, 160)
        h = rng.randint(1, 120)
        cases.append((w, h, random_ops(rng)))

    failures = 0
    start = time.time()
    for idx, (w, h, (color, geo, flt)) in enumerate(cases):
        bmp = make_bmp(w, h, rng)
        legacy_ops = color + geo + flt
        config = {"color": color, "geometry": geo, "filter": flt}

        old_bytes, old_meta = process_image(bmp, legacy_ops)
        new_bytes, new_meta = build_pipeline(config).run(bmp)

        label = "%02d %dx%d ops=%s" % (idx, w, h, [o["name"] for o in legacy_ops])
        if old_bytes != new_bytes:
            failures += 1
            first = next(
                (k for k in range(min(len(old_bytes), len(new_bytes)))
                 if old_bytes[k] != new_bytes[k]), None)
            print("FAIL %s byte@%s old=%s new=%s lens=(%d,%d)"
                  % (label, first,
                     None if first is None else old_bytes[first],
                     None if first is None else new_bytes[first],
                     len(old_bytes), len(new_bytes)))
            continue
        if old_meta != new_meta:
            failures += 1
            print("FAIL %s metadata old=%s new=%s" % (label, old_meta, new_meta))
            continue
        print("PASS %s" % label)

    print("\n%d cases, %d failures, %.1fs (seed=%d)"
          % (len(cases), failures, time.time() - start, seed))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
