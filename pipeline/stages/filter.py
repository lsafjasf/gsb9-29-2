"""滤波阶段：boxblur / sharpen，3x3 邻域，边界像素保持原样。

邻域滤波必须读原图写新图，因此总是写入从池里申请的新缓冲（事务式），
成功后归还旧缓冲；失败则归还新缓冲、保留旧缓冲。
"""
from ..buffers import ImageBuffer


def _filtered(buf, pool, compute):
    n = buf.nbytes
    out = pool.acquire(n)
    try:
        out[:n] = buf.data[:n]  # 边界直接保留
        compute(buf.data, out, buf.width, buf.height)
    except Exception:
        pool.release(out)
        raise
    pool.release(buf.data)
    return ImageBuffer(out, buf.width, buf.height, buf.channels, buf.metadata)


def _boxblur(buf, pool):
    def compute(px, out, w, h):
        row = w * 3
        for y in range(1, h - 1):
            base = y * row
            for x in range(1, w - 1):
                i = base + x * 3
                for c in range(3):
                    j = i + c
                    s = (px[j - row - 3] + px[j - row] + px[j - row + 3]
                         + px[j - 3] + px[j] + px[j + 3]
                         + px[j + row - 3] + px[j + row] + px[j + row + 3])
                    out[j] = s // 9
    return _filtered(buf, pool, compute)


def _sharpen(buf, pool):
    def compute(px, out, w, h):
        row = w * 3
        for y in range(1, h - 1):
            base = y * row
            for x in range(1, w - 1):
                i = base + x * 3
                for c in range(3):
                    j = i + c
                    v = px[j] * 5 - px[j - row] - px[j + row] - px[j - 3] - px[j + 3]
                    out[j] = 0 if v < 0 else (255 if v > 255 else v)
    return _filtered(buf, pool, compute)


_OPS = {
    "boxblur": _boxblur,
    "sharpen": _sharpen,
}


class FilterStage:
    name = "filter"

    def __init__(self, ops):
        for op in ops:
            if op.get("name") not in _OPS:
                raise ValueError("unknown filter op: %r" % (op.get("name"),))
        self.ops = list(ops)

    def run(self, buf, pool):
        for op in self.ops:
            buf = _OPS[op["name"]](buf, pool)
            buf.metadata["ops"].append(op["name"])
        return buf
