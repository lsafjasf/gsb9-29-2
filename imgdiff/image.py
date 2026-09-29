"""Lightweight raster container plus a generator for the synthetic test chart."""

import math


class Image:
    def __init__(self, width, height, mode, pixels, metadata=None):
        if mode not in ("L", "LA", "RGB", "RGBA"):
            raise ValueError("unsupported mode %r" % mode)
        self.width = width
        self.height = height
        self.mode = mode
        self.pixels = bytes(pixels)
        self.metadata = dict(metadata or {})

    @property
    def channels(self):
        return len(self.mode)

    def get_pixel(self, x, y):
        off = (y * self.width + x) * self.channels
        return tuple(self.pixels[off:off + self.channels])

    def set_pixel(self, x, y, value):
        off = (y * self.width + x) * self.channels
        mutable = bytearray(self.pixels)
        mutable[off:off + self.channels] = bytes(value)
        self.pixels = bytes(mutable)

    def copy(self):
        return Image(self.width, self.height, self.mode, self.pixels,
                     metadata={"color_type": self.metadata.get("color_type"),
                               "text": dict(self.metadata.get("text", {})),
                               "orientation": self.metadata.get("orientation")})

    @property
    def orientation(self):
        return self.metadata.get("orientation")

    def with_orientation(self, value):
        clone = self.copy()
        clone.metadata["orientation"] = value
        clone.metadata["text"]["Orientation"] = str(value)
        return clone


def make_chart(width=64, height=64, seed=7):
    """Deterministic structured RGB test chart (gradients, bands, noise)."""
    state = seed
    pixels = bytearray(width * height * 3)

    def rnd():
        # Deterministic LCG so two environments reproduce the same input.
        nonlocal state
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        return state / 0x7FFFFFFF

    def clamp(value):
        return 0 if value < 0 else 255 if value > 255 else int(value)

    for y in range(height):
        for x in range(width):
            r = 120 + 80 * math.sin(x / 9.0) + 22 * (rnd() - 0.5)
            g = 110 + 60 * math.cos(y / 8.0) + 22 * (rnd() - 0.5)
            b = 128 + 70 * math.sin((x + y) / 11.0) + 22 * (rnd() - 0.5)
            if x < width // 4:  # hard band: exercises block-edge/ringing
                r += 28
            off = (y * width + x) * 3
            pixels[off] = clamp(r)
            pixels[off + 1] = clamp(g)
            pixels[off + 2] = clamp(b)
    img = Image(width, height, "RGB", pixels,
                metadata={"color_type": 2, "text": {"Orientation": "1"},
                          "orientation": 1})
    return img
