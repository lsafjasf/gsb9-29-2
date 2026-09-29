"""Minimal 8-bit PNG codec using only the standard library (zlib + struct).

Supported on read: 8-bit grayscale, grayscale+alpha, RGB, RGBA, indexed color
(color type 0/2/3/4/6, bit depth 8 only, no interlace).
Written images are always 8-bit, color type 0/2/6, no interlace.

tEXt chunks are preserved into Image.metadata (key -> str). Orientation is
read/written under the tEXt key "Orientation" (1..8), matching the value space
of EXIF orientation tags.
"""

import struct
import zlib

PNG_SIG = b"\x89PNG\r\n\x1a\n"


class PNGError(ValueError):
    pass


def _paeth(a, b, c):
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(raw, width, height, channels):
    stride = width * channels
    out = bytearray(stride * height)
    pos = 0
    prev = bytearray(stride)
    for row in range(height):
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if ftype == 1:  # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:  # Average
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                up = prev[i]
                up_left = prev[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(left, up, up_left)) & 0xFF
        elif ftype != 0:
            raise PNGError("unsupported PNG filter type %d" % ftype)
        out[row * stride:(row + 1) * stride] = line
        prev = line
    return bytes(out)


def _filter(data, width, height, channels):
    stride = width * channels
    out = bytearray()
    prev = bytes(stride)
    for row in range(height):
        line = data[row * stride:(row + 1) * stride]
        # Adaptive filtering: pick per-row filter with smallest sum of
        # absolute filtered bytes (a cheap heuristic, good enough here).
        candidates = []
        none = bytes(line)
        candidates.append((0, none))
        sub = bytearray(stride)
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            sub[i] = (line[i] - left) & 0xFF
        candidates.append((1, bytes(sub)))
        up = bytes((line[i] - prev[i]) & 0xFF for i in range(stride))
        candidates.append((2, up))
        avg = bytearray(stride)
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            avg[i] = (line[i] - ((left + prev[i]) >> 1)) & 0xFF
        candidates.append((3, bytes(avg)))
        pa = bytearray(stride)
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up_left = prev[i - channels] if i >= channels else 0
            pa[i] = (line[i] - _paeth(left, prev[i], up_left)) & 0xFF
        candidates.append((4, bytes(pa)))
        ftype, filtered = min(candidates, key=lambda c: sum(c[1]))
        out.append(ftype)
        out.extend(filtered)
        prev = line
    return bytes(out)


def _chunk(tag, payload):
    return (struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))


def _parse_text(payload):
    key, sep, value = payload.partition(b"\x00")
    if not sep:
        return None
    return key.decode("latin-1"), value.decode("latin-1")


def loads(blob):
    """Decode PNG bytes -> Image (see image.Image)."""
    from .image import Image

    if not blob.startswith(PNG_SIG):
        raise PNGError("not a PNG file (bad signature)")
    pos = len(PNG_SIG)
    width = height = bit_depth = color_type = None
    palette = None
    transparency = None
    text = {}
    idat = bytearray()
    while pos + 8 <= len(blob):
        (length,) = struct.unpack(">I", blob[pos:pos + 4])
        tag = blob[pos + 4:pos + 8]
        payload = blob[pos + 8:pos + 8 + length]
        if len(payload) != length:
            raise PNGError("truncated PNG chunk %r" % tag)
        if tag == b"IHDR":
            (width, height, bit_depth, color_type, comp, filt, interlace) = \
                struct.unpack(">IIBBBBB", payload)
            if comp != 0 or filt != 0:
                raise PNGError("unsupported PNG compression/filter method")
            if interlace != 0:
                raise PNGError("interlaced PNG is not supported")
        elif tag == b"PLTE":
            palette = [tuple(payload[i:i + 3]) for i in range(0, len(payload), 3)]
        elif tag == b"tRNS":
            transparency = payload
        elif tag == b"tEXt":
            parsed = _parse_text(payload)
            if parsed:
                text[parsed[0]] = parsed[1]
        elif tag == b"IDAT":
            idat.extend(payload)
        pos += 12 + length
        if tag == b"IEND":
            break
    if width is None:
        raise PNGError("missing IHDR chunk")
    if bit_depth != 8:
        raise PNGError("only 8-bit PNG is supported (got bit depth %d)"
                       % bit_depth)

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        raise PNGError("unsupported PNG color type %d" % color_type)

    raw = zlib.decompress(bytes(idat))
    decoded = _unfilter(raw, width, height, channels)

    if color_type == 3:
        if palette is None:
            raise PNGError("indexed PNG without PLTE")
        pixels = bytearray(width * height * 3)
        for idx, value in enumerate(decoded):
            rgb = palette[value]
            off = idx * 3
            pixels[off:off + 3] = bytes(rgb)
        mode = "RGB"
        if transparency:
            rgba = bytearray(width * height * 4)
            for idx in range(width * height):
                rgba[idx * 4:idx * 4 + 3] = pixels[idx * 3:idx * 3 + 3]
                entry = decoded[idx]
                rgba[idx * 4 + 3] = transparency[entry] if entry < len(transparency) else 255
            pixels = rgba
            mode = "RGBA"
    elif color_type == 0:
        pixels, mode = decoded, "L"
    elif color_type == 2:
        pixels, mode = decoded, "RGB"
    elif color_type == 4:
        pixels, mode = decoded, "LA"
    else:
        pixels, mode = decoded, "RGBA"

    orientation = None
    if "Orientation" in text:
        try:
            orientation = int(text["Orientation"])
        except ValueError:
            orientation = None
    return Image(width, height, mode, bytes(pixels),
                 metadata={"color_type": color_type, "text": dict(text),
                           "orientation": orientation})


def load(path):
    with open(path, "rb") as handle:
        return loads(handle.read())


def dumps(image):
    """Encode an 8-bit Image to PNG bytes."""
    color_type = {"L": 0, "RGB": 2, "RGBA": 6, "LA": 4}[image.mode]
    channels = len(image.mode)
    if image.width * image.height * channels != len(image.pixels):
        raise PNGError("pixel buffer size does not match dimensions")
    header = struct.pack(">IIBBBBB", image.width, image.height, 8,
                         color_type, 0, 0, 0)
    parts = [PNG_SIG, _chunk(b"IHDR", header)]
    text = dict(image.metadata.get("text", {}))
    orientation = image.metadata.get("orientation")
    if orientation is not None and "Orientation" not in text:
        text["Orientation"] = str(orientation)
    for key in sorted(text):
        payload = key.encode("latin-1") + b"\x00" + str(text[key]).encode("latin-1")
        parts.append(_chunk(b"tEXt", payload))
    filtered = _filter(image.pixels, image.width, image.height, channels)
    parts.append(_chunk(b"IDAT", zlib.compress(filtered, 6)))
    parts.append(_chunk(b"IEND", b""))
    return b"".join(parts)


def dump(image, path):
    with open(path, "wb") as handle:
        handle.write(dumps(image))
