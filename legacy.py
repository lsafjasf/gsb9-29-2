"""重构前的图像处理代码（基准实现，冻结不改）。

整个处理流程（解码、色彩、几何、滤波、编码）都塞在 process_image 一个
函数里，步骤之间共享 buf / w / h / ch / tmp / meta / history 等中间变量，
想改某一步必须通读全文。重构后的管线版本见 imgpipe/，两者行为由
tests/test_differential.py 逐字节对拍保证等价。

容器格式 GSB1：
    MAGIC(4B "GSB1") + header_len(uint32 LE) + header(JSON) + 原始像素
    header = {"width", "height", "channels", "meta"}
    JSON 序列化固定 sort_keys + 紧凑分隔符，保证编码输出确定。
"""

import json
import struct

MAGIC = b"GSB1"


def _dump_header(header):
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")


def process_image(blob, ops):
    # ---------------- 解码 ----------------
    if len(blob) < 8 or blob[:4] != MAGIC:
        raise ValueError("bad magic")
    (hlen,) = struct.unpack_from("<I", blob, 4)
    header = json.loads(blob[8:8 + hlen].decode("utf-8"))
    w = header["width"]
    h = header["height"]
    ch = header["channels"]
    meta = header.get("meta", {})
    history = list(meta.get("history", []))
    buf = bytearray(blob[8 + hlen:8 + hlen + w * h * ch])
    if len(buf) != w * h * ch:
        raise ValueError("truncated payload")

    # ---------------- 色彩 ----------------
    if ops.get("grayscale") and ch == 3:
        i = 0
        while i < len(buf):
            g = (buf[i] * 299 + buf[i + 1] * 587 + buf[i + 2] * 114) // 1000
            buf[i] = g
            buf[i + 1] = g
            buf[i + 2] = g
            i += 3
        history.append("grayscale")
    if "brightness" in ops:
        delta = ops["brightness"]
        for i in range(len(buf)):
            v = buf[i] + delta
            if v < 0:
                v = 0
            elif v > 255:
                v = 255
            buf[i] = v
        history.append("brightness")
    if ops.get("invert"):
        for i in range(len(buf)):
            buf[i] = 255 - buf[i]
        history.append("invert")

    # ---------------- 几何 ----------------
    if "crop" in ops:
        x0, y0, cw, hh = ops["crop"]
        if x0 < 0 or y0 < 0 or cw <= 0 or hh <= 0 or x0 + cw > w or y0 + hh > h:
            raise ValueError("crop out of range")
        tmp = bytearray(cw * hh * ch)
        for y in range(hh):
            src = ((y0 + y) * w + x0) * ch
            tmp[y * cw * ch:(y + 1) * cw * ch] = buf[src:src + cw * ch]
        buf = tmp
        w = cw
        h = hh
        history.append("crop")
    if ops.get("flip_h"):
        tmp = bytearray(w * h * ch)
        for y in range(h):
            for x in range(w):
                s = (y * w + x) * ch
                d = (y * w + (w - 1 - x)) * ch
                tmp[d:d + ch] = buf[s:s + ch]
        buf = tmp
        history.append("flip_h")
    if ops.get("flip_v"):
        tmp = bytearray(w * h * ch)
        row = w * ch
        for y in range(h):
            tmp[(h - 1 - y) * row:(h - y) * row] = buf[y * row:(y + 1) * row]
        buf = tmp
        history.append("flip_v")
    if ops.get("rotate90"):
        tmp = bytearray(w * h * ch)
        for y in range(h):
            for x in range(w):
                s = (y * w + x) * ch
                d = (x * h + (h - 1 - y)) * ch
                tmp[d:d + ch] = buf[s:s + ch]
        buf = tmp
        w, h = h, w
        history.append("rotate90")

    # ---------------- 滤波 ----------------
    if "blur" in ops:
        r = ops["blur"]
        tmp = bytearray(w * h * ch)
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
                            s += buf[base + xx * ch + c]
                    tmp[(y * w + x) * ch + c] = s // n
        buf = tmp
        history.append("blur")

    # ---------------- 编码 ----------------
    meta["history"] = history
    header = {"width": w, "height": h, "channels": ch, "meta": meta}
    hb = _dump_header(header)
    return MAGIC + struct.pack("<I", len(hb)) + hb + bytes(buf)
