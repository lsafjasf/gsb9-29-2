"""GSB1 容器格式的解码 / 编码（与 legacy.py 字节级一致）。"""

import json
import struct

from .buffer import MAGIC, ImageBuffer


def dump_header(header):
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode_blob(blob, pool):
    """bytes -> ImageBuffer。像素缓冲从池中获取，便于后续阶段/下次运行复用。"""
    if len(blob) < 8 or blob[:4] != MAGIC:
        raise ValueError("bad magic")
    (hlen,) = struct.unpack_from("<I", blob, 4)
    header = json.loads(blob[8:8 + hlen].decode("utf-8"))
    width = header["width"]
    height = header["height"]
    channels = header["channels"]
    meta = dict(header.get("meta", {}))
    meta["history"] = list(meta.get("history", []))
    size = width * height * channels
    data = pool.acquire(size)
    data[:] = blob[8 + hlen:8 + hlen + size]
    if len(data) != size:
        pool.release(data)
        raise ValueError("truncated payload")
    return ImageBuffer(width, height, channels, data, meta)


def encode_buffer(buf):
    """ImageBuffer -> bytes。输出是新分配的 bytes，调用方持有。"""
    header = {
        "width": buf.width,
        "height": buf.height,
        "channels": buf.channels,
        "meta": buf.meta,
    }
    hb = dump_header(header)
    return MAGIC + struct.pack("<I", len(hb)) + hb + bytes(buf.data)


def parse_header(blob):
    """测试辅助：只解析头部，用于单独比较元数据。"""
    (hlen,) = struct.unpack_from("<I", blob, 4)
    return json.loads(blob[8:8 + hlen].decode("utf-8"))
