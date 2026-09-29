"""测试公共工具：构造确定性的 GSB1 图像。"""

import struct

from imgpipe.buffer import MAGIC
from imgpipe.codec import dump_header


def make_blob(rng, width, height, channels, meta=None):
    header = {
        "width": width,
        "height": height,
        "channels": channels,
        "meta": meta if meta is not None else {},
    }
    hb = dump_header(header)
    payload = rng.randbytes(width * height * channels)
    return MAGIC + struct.pack("<I", len(hb)) + hb + payload
