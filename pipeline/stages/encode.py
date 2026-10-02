"""编码阶段：ImageBuffer -> (BMP 文件字节, 元数据字典)。"""
import struct


class EncodeStage:
    name = "encode"

    def run(self, buf, pool):
        w, h = buf.width, buf.height
        stride = (w * 3 + 3) // 4 * 4
        img_size = stride * h
        out = bytearray(54 + img_size)
        struct.pack_into("<2sIHHI", out, 0, b"BM", 54 + img_size, 0, 0, 54)
        struct.pack_into("<IiiHHIIiiII", out, 14,
                         40, w, h, 1, 24, 0, img_size, 2835, 2835, 0, 0)
        px = buf.data
        row_bytes = w * 3
        for y in range(h):
            src_base = (h - 1 - y) * row_bytes
            dst_base = 54 + y * stride
            # RGB -> BGR，整行按通道重排；行尾填充字节保持为 0
            out[dst_base:dst_base + row_bytes:3] = px[src_base + 2:src_base + row_bytes:3]
            out[dst_base + 1:dst_base + row_bytes:3] = px[src_base + 1:src_base + row_bytes:3]
            out[dst_base + 2:dst_base + row_bytes:3] = px[src_base:src_base + row_bytes:3]
        pool.release(buf.data)  # 最后一帧已拷贝为输出字节，归还缓冲池
        meta = {
            "format": "BMP24",
            "width": w,
            "height": h,
            "channels": buf.channels,
            "ops": list(buf.metadata["ops"]),
        }
        return bytes(out), meta
