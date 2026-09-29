"""五个显式阶段：解码、色彩、几何、滤波、编码。

约定：
- 每个阶段输入一个 ImageBuffer、输出一个 ImageBuffer（色彩阶段原地修改，
  几何/滤波阶段产出新缓冲），阶段间只通过 ImageBuffer 交接；
- 需要新缓冲时从 BufferPool 取；旧缓冲在新缓冲就绪后立即归还池中，
  因此任意时刻最多同时存在 2 个全尺寸像素缓冲；
- 子操作的开关与顺序、算术（含取整/截断方式）与 legacy.process_image
  完全一致，由 tests/test_differential.py 逐字节对拍保证。
"""

from .buffer import ImageBuffer
from .codec import decode_blob, encode_buffer


class DecodeStage:
    name = "decode"

    def apply_blob(self, blob, pool):
        return decode_blob(blob, pool)


class ColorStage:
    """灰度 / 亮度 / 反色：全部原地修改，零额外缓冲。"""

    name = "color"

    def apply(self, buf, ops, pool):
        data = buf.data
        if ops.get("grayscale") and buf.channels == 3:
            i = 0
            while i < len(data):
                g = (data[i] * 299 + data[i + 1] * 587 + data[i + 2] * 114) // 1000
                data[i] = g
                data[i + 1] = g
                data[i + 2] = g
                i += 3
            buf.meta["history"].append("grayscale")
        if "brightness" in ops:
            delta = ops["brightness"]
            for i in range(len(data)):
                v = data[i] + delta
                if v < 0:
                    v = 0
                elif v > 255:
                    v = 255
                data[i] = v
            buf.meta["history"].append("brightness")
        if ops.get("invert"):
            for i in range(len(data)):
                data[i] = 255 - data[i]
            buf.meta["history"].append("invert")
        return buf


class GeometryStage:
    """裁剪 / 水平翻转 / 垂直翻转 / 旋转90：每个子操作产出一个新缓冲。"""

    name = "geometry"

    def apply(self, buf, ops, pool):
        if "crop" in ops:
            buf = self._crop(buf, ops["crop"], pool)
        if ops.get("flip_h"):
            buf = self._flip_h(buf, pool)
        if ops.get("flip_v"):
            buf = self._flip_v(buf, pool)
        if ops.get("rotate90"):
            buf = self._rotate90(buf, pool)
        return buf

    @staticmethod
    def _replace(buf, width, height, out, pool, op_name):
        """旧缓冲归还池，返回携带同一 meta 的新缓冲。"""
        meta = buf.meta
        pool.release(buf.data)
        meta["history"].append(op_name)
        return ImageBuffer(width, height, buf.channels, out, meta)

    def _crop(self, buf, rect, pool):
        x0, y0, cw, hh = rect
        if x0 < 0 or y0 < 0 or cw <= 0 or hh <= 0 \
                or x0 + cw > buf.width or y0 + hh > buf.height:
            raise ValueError("crop out of range")
        ch = buf.channels
        src = buf.data
        out = pool.acquire(cw * hh * ch)
        for y in range(hh):
            s = ((y0 + y) * buf.width + x0) * ch
            out[y * cw * ch:(y + 1) * cw * ch] = src[s:s + cw * ch]
        return self._replace(buf, cw, hh, out, pool, "crop")

    def _flip_h(self, buf, pool):
        w, h, ch = buf.width, buf.height, buf.channels
        src = buf.data
        out = pool.acquire(w * h * ch)
        for y in range(h):
            for x in range(w):
                s = (y * w + x) * ch
                d = (y * w + (w - 1 - x)) * ch
                out[d:d + ch] = src[s:s + ch]
        return self._replace(buf, w, h, out, pool, "flip_h")

    def _flip_v(self, buf, pool):
        w, h, ch = buf.width, buf.height, buf.channels
        src = buf.data
        out = pool.acquire(w * h * ch)
        row = w * ch
        for y in range(h):
            out[(h - 1 - y) * row:(h - y) * row] = src[y * row:(y + 1) * row]
        return self._replace(buf, w, h, out, pool, "flip_v")

    def _rotate90(self, buf, pool):
        w, h, ch = buf.width, buf.height, buf.channels
        src = buf.data
        out = pool.acquire(w * h * ch)
        for y in range(h):
            for x in range(w):
                s = (y * w + x) * ch
                d = (x * h + (h - 1 - y)) * ch
                out[d:d + ch] = src[s:s + ch]
        return self._replace(buf, h, w, out, pool, "rotate90")


class FilterStage:
    """盒式模糊：产出一个新缓冲，峰值 = 输入 + 输出两个全尺寸缓冲。"""

    name = "filter"

    def apply(self, buf, ops, pool):
        if "blur" not in ops:
            return buf
        r = ops["blur"]
        w, h, ch = buf.width, buf.height, buf.channels
        src = buf.data
        out = pool.acquire(w * h * ch)
        for y in range(h):
            y0 = y - r
            if y0 < 0:
                y0 = 0
            y1 = y + r
            if y1 > h - 1:
                y1 = h - 1
            for x in range(w):
                x0 = x - r
                if x0 < 0:
                    x0 = 0
                x1 = x + r
                if x1 > w - 1:
                    x1 = w - 1
                n = (y1 - y0 + 1) * (x1 - x0 + 1)
                for c in range(ch):
                    s = 0
                    for yy in range(y0, y1 + 1):
                        base = yy * w * ch
                        for xx in range(x0, x1 + 1):
                            s += src[base + xx * ch + c]
                    out[(y * w + x) * ch + c] = s // n
        meta = buf.meta
        pool.release(buf.data)
        meta["history"].append("blur")
        return ImageBuffer(w, h, ch, out, meta)


class EncodeStage:
    name = "encode"

    def apply_buf(self, buf):
        return encode_buffer(buf)
