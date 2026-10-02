"""几何阶段：flip_h（就地）/ rotate90（换形，写入新缓冲）。

rotate90 是事务式的：结果先写进从池里申请的新缓冲，成功后才归还旧缓冲；
中途失败则归还新缓冲、保留旧缓冲，调用方看到的是失败前的完好状态。
"""
from ..buffers import ImageBuffer


def _flip_h(buf):
    w = buf.width
    row = w * 3
    px = buf.data
    for y in range(buf.height):
        base = y * row
        src = px[base:base + row]
        px[base:base + row:3] = src[0::3][::-1]
        px[base + 1:base + row:3] = src[1::3][::-1]
        px[base + 2:base + row:3] = src[2::3][::-1]


def _rotate90(buf, pool):
    """顺时针 90 度：new_w = h, new_h = w。"""
    w, h = buf.width, buf.height
    nw = h
    n = buf.nbytes
    out = pool.acquire(n)
    try:
        src = buf.data
        step = nw * 3
        for y in range(h):
            srow = y * w * 3
            start = (h - 1 - y) * 3
            end = start + (w - 1) * step + 1
            out[start:end:step] = src[srow:srow + w * 3:3]
            out[start + 1:end + 1:step] = src[srow + 1:srow + w * 3:3]
            out[start + 2:end + 2:step] = src[srow + 2:srow + w * 3:3]
    except Exception:
        pool.release(out)
        raise
    pool.release(buf.data)
    return ImageBuffer(out, nw, w, buf.channels, buf.metadata)


class GeometryStage:
    name = "geometry"

    def __init__(self, ops):
        for op in ops:
            if op.get("name") not in ("flip_h", "rotate90"):
                raise ValueError("unknown geometry op: %r" % (op.get("name"),))
        self.ops = list(ops)

    def run(self, buf, pool):
        for op in self.ops:
            if op["name"] == "flip_h":
                _flip_h(buf)
            else:
                buf = _rotate90(buf, pool)
            buf.metadata["ops"].append(op["name"])
        return buf
