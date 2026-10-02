"""重构前的图像处理代码：所有步骤都堆在 process_image 这一个大函数里。

该文件是差分对拍的基线（baseline），保留它的行为原样不动。
输入 24 位未压缩 BMP，输出 (BMP 字节, 元数据字典)。
"""
import struct


def process_image(data, ops):
    # ================= 解码 BMP（与后面的处理共享 w/h/px 等变量）=================
    if len(data) < 54 or data[0:2] != b"BM":
        raise ValueError("not a BMP file")
    off = struct.unpack_from("<I", data, 10)[0]
    dib = struct.unpack_from("<I", data, 14)[0]
    if dib < 40:
        raise ValueError("unsupported DIB header size %d" % dib)
    w = struct.unpack_from("<i", data, 18)[0]
    hraw = struct.unpack_from("<i", data, 22)[0]
    bpp = struct.unpack_from("<H", data, 28)[0]
    comp = struct.unpack_from("<I", data, 30)[0]
    if w <= 0 or hraw == 0:
        raise ValueError("bad dimensions")
    if bpp != 24 or comp != 0:
        raise ValueError("only uncompressed 24-bit BMP supported")
    top_down = hraw < 0
    h = hraw if hraw > 0 else -hraw
    stride = (w * 3 + 3) // 4 * 4
    if len(data) < off + stride * h:
        raise ValueError("truncated BMP data")

    px = bytearray(w * h * 3)  # 内部统一 RGB
    for y in range(h):
        sy = y if top_down else h - 1 - y
        row = off + sy * stride
        for x in range(w):
            si = row + x * 3
            di = (y * w + x) * 3
            px[di] = data[si + 2]
            px[di + 1] = data[si + 1]
            px[di + 2] = data[si]

    applied = []
    tmp = bytearray(0)  # 多个 op 复用的临时缓冲，谁都在用
    i = j = 0
    r = g = b = 0

    # ================= 一串处理步骤，全靠改 w/h/px/tmp 传递 =================
    for op in ops:
        name = op["name"]
        if name == "invert":
            i = 0
            while i < len(px):
                px[i] = 255 - px[i]
                i += 1
        elif name == "grayscale":
            i = 0
            while i < len(px):
                g = (px[i] * 299 + px[i + 1] * 587 + px[i + 2] * 114) // 1000
                px[i] = px[i + 1] = px[i + 2] = g
                i += 3
        elif name == "sepia":
            i = 0
            while i < len(px):
                r, g, b = px[i], px[i + 1], px[i + 2]
                tr = (r * 393 + g * 769 + b * 189) // 1000
                tg = (r * 349 + g * 686 + b * 168) // 1000
                tb = (r * 272 + g * 534 + b * 131) // 1000
                px[i] = tr if tr < 255 else 255
                px[i + 1] = tg if tg < 255 else 255
                px[i + 2] = tb if tb < 255 else 255
                i += 3
        elif name == "flip_h":
            for y in range(h):
                base = y * w * 3
                for x in range(w // 2):
                    a = base + x * 3
                    z = base + (w - 1 - x) * 3
                    j = 0
                    while j < 3:
                        px[a + j], px[z + j] = px[z + j], px[a + j]
                        j += 1
        elif name == "rotate90":
            # 顺时针 90 度；new_w = h, new_h = w
            tmp = bytearray(w * h * 3)
            nw, nh = h, w
            y = 0
            while y < h:
                x = 0
                while x < w:
                    si = (y * w + x) * 3
                    di = (x * nw + (h - 1 - y)) * 3
                    tmp[di] = px[si]
                    tmp[di + 1] = px[si + 1]
                    tmp[di + 2] = px[si + 2]
                    x += 1
                y += 1
            px = tmp
            w, h = nw, nh
        elif name == "boxblur":
            # 3x3 均值，边界像素保持原样
            tmp = bytearray(px)
            y = 1
            while y < h - 1:
                x = 1
                while x < w - 1:
                    c = 0
                    while c < 3:
                        i = (y * w + x) * 3 + c
                        s = 0
                        dy = -1
                        while dy <= 1:
                            dx = -1
                            while dx <= 1:
                                s += px[((y + dy) * w + (x + dx)) * 3 + c]
                                dx += 1
                            dy += 1
                        tmp[i] = s // 9
                        c += 1
                    x += 1
                y += 1
            px = tmp
        elif name == "sharpen":
            tmp = bytearray(px)
            y = 1
            while y < h - 1:
                x = 1
                while x < w - 1:
                    c = 0
                    while c < 3:
                        i = (y * w + x) * 3 + c
                        v = (px[i] * 5 - px[i - w * 3] - px[i + w * 3]
                             - px[i - 3] - px[i + 3])
                        tmp[i] = 0 if v < 0 else (255 if v > 255 else v)
                        c += 1
                    x += 1
                y += 1
            px = tmp
        else:
            raise ValueError("unknown op: %r" % (name,))
        applied.append(name)

    # ================= 编码回 BMP =================
    stride = (w * 3 + 3) // 4 * 4
    imgsize = stride * h
    out = bytearray(54 + imgsize)
    struct.pack_into("<2sIHHI", out, 0, b"BM", 54 + imgsize, 0, 0, 54)
    struct.pack_into("<IiiHHIIiiII", out, 14,
                     40, w, h, 1, 24, 0, imgsize, 2835, 2835, 0, 0)
    pad = b"\x00" * (stride - w * 3)
    for y in range(h):
        sy = h - 1 - y
        drow = 54 + y * stride
        for x in range(w):
            si = (sy * w + x) * 3
            di = drow + x * 3
            out[di] = px[si + 2]
            out[di + 1] = px[si + 1]
            out[di + 2] = px[si]
        if pad:
            out[drow + w * 3:drow + stride] = pad

    meta = {
        "format": "BMP24",
        "width": w,
        "height": h,
        "channels": 3,
        "ops": applied,
    }
    return bytes(out), meta
