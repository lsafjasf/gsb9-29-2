"""解码阶段：BMP 文件字节 -> ImageBuffer（RGB，无行对齐）。"""
import struct
from ..buffers import ImageBuffer


class DecodeStage:
    name = "decode"

    def run(self, item, pool):
        data = item
        if len(data) < 54 or data[0:2] != b"BM":
            raise ValueError("not a BMP file")
        offset = struct.unpack_from("<I", data, 10)[0]
        dib = struct.unpack_from("<I", data, 14)[0]
        if dib < 40:
            raise ValueError("unsupported DIB header size %d" % dib)
        width = struct.unpack_from("<i", data, 18)[0]
        height_raw = struct.unpack_from("<i", data, 22)[0]
        bpp = struct.unpack_from("<H", data, 28)[0]
        compression = struct.unpack_from("<I", data, 30)[0]
        if width <= 0 or height_raw == 0:
            raise ValueError("bad dimensions")
        if bpp != 24 or compression != 0:
            raise ValueError("only uncompressed 24-bit BMP supported")
        top_down = height_raw < 0
        height = height_raw if height_raw > 0 else -height_raw
        stride = (width * 3 + 3) // 4 * 4
        if len(data) < offset + stride * height:
            raise ValueError("truncated BMP data")

        px = pool.acquire(width * height * 3)
        row_bytes = width * 3
        for y in range(height):
            src_y = y if top_down else height - 1 - y
            seg = data[offset + src_y * stride: offset + src_y * stride + row_bytes]
            base = y * row_bytes
            # BGR -> RGB，整行按通道重排
            px[base:base + row_bytes:3] = seg[2::3]
            px[base + 1:base + row_bytes:3] = seg[1::3]
            px[base + 2:base + row_bytes:3] = seg[0::3]
        return ImageBuffer(px, width, height, 3, {"ops": []})
