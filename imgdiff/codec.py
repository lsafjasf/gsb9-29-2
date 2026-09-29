"""Deterministic lossy codec stage used to model pipeline round-trips.

The transform chain (a textbook JPEG-like intra-only pipeline):

  RGB -> BT.601 full-range YCbCr (integer rounding)
       -> 4:2:0 chroma subsampling (averaging)
       -> 8x8 orthonormal DCT + scalar quantization
       -> inverse of each step -> RGB (integer rounding)

Two environment profiles are provided:

  env A: round-half-up color math, box-average chroma subsampling
  env B: round-half-even color math, corner-sampled chroma subsampling

Both are legitimate implementations, yet pixels disagree in boundary cases
-- exactly the "looks the same to the eye, downstream occasionally fails"
situation the checker is meant to diagnose. Images are padded to a multiple
of 8 by edge replication, then cropped back.
"""

import math

BLOCK = 8


def _build_dct():
    basis = [[0.0] * BLOCK for _ in range(BLOCK)]
    for u in range(BLOCK):
        scale = math.sqrt(1.0 / BLOCK) if u == 0 else math.sqrt(2.0 / BLOCK)
        for x in range(BLOCK):
            basis[u][x] = scale * math.cos(math.pi * (2 * x + 1) * u
                                           / (2 * BLOCK))
    transpose = [[basis[u][x] for u in range(BLOCK)] for x in range(BLOCK)]
    return basis, transpose


_A, _AT = _build_dct()


def _matmul(left, right):
    return [[sum(left[i][k] * right[k][j] for k in range(BLOCK))
             for j in range(BLOCK)] for i in range(BLOCK)]


def _round_half_up(value):
    return int(math.floor(value + 0.5))


def _round_half_even(value):
    # Only differs from half-up at exact *.5 values.
    base = int(math.floor(value))
    frac = value - base
    if frac < 0.5:
        return base
    if frac > 0.5:
        return base + 1
    return base if base % 2 == 0 else base + 1


def _clamp(value):
    if value < 0:
        return 0
    if value > 255:
        return 255
    return value


def _split_planes(pixels, width, height):
    y_plane = bytearray(width * height)
    cb_plane = bytearray(width * height)
    cr_plane = bytearray(width * height)
    for i in range(width * height):
        off = i * 3
        y_plane[i], cb_plane[i], cr_plane[i] = pixels[off:off + 3]
    return y_plane, cb_plane, cr_plane


def _merge_planes(y_plane, cb_plane, cr_plane):
    pixels = bytearray(len(y_plane) * 3)
    for i in range(len(y_plane)):
        off = i * 3
        pixels[off] = y_plane[i]
        pixels[off + 1] = cb_plane[i]
        pixels[off + 2] = cr_plane[i]
    return bytes(pixels)


def _rgb_to_ycbcr(image, rounder):
    pixels = image.pixels
    width, height = image.width, image.height
    y_plane = bytearray(width * height)
    cb_plane = bytearray(width * height)
    cr_plane = bytearray(width * height)
    for i in range(width * height):
        off = i * 3
        r, g, b = pixels[off], pixels[off + 1], pixels[off + 2]
        y_plane[i] = _clamp(rounder(0.299 * r + 0.587 * g + 0.114 * b))
        cb_plane[i] = _clamp(rounder(128 - 0.168736 * r - 0.331264 * g
                                     + 0.5 * b))
        cr_plane[i] = _clamp(rounder(128 + 0.5 * r - 0.418688 * g
                                     - 0.081312 * b))
    return y_plane, cb_plane, cr_plane


def _ycbcr_to_rgb(y_plane, cb_plane, cr_plane, rounder):
    pixels = bytearray(len(y_plane) * 3)
    for i in range(len(y_plane)):
        yv = y_plane[i]
        cb = cb_plane[i] - 128
        cr = cr_plane[i] - 128
        off = i * 3
        pixels[off] = _clamp(rounder(yv + 1.402 * cr))
        pixels[off + 1] = _clamp(rounder(yv - 0.344136 * cb
                                         - 0.714136 * cr))
        pixels[off + 2] = _clamp(rounder(yv + 1.772 * cb))
    return bytes(pixels)


def _subsample(plane, width, height, env):
    half_w = width // 2
    half_h = height // 2
    out = bytearray(half_w * half_h)
    for y in range(half_h):
        for x in range(half_w):
            i00 = plane[(2 * y) * width + 2 * x]
            if env == "A":
                i01 = plane[(2 * y) * width + 2 * x + 1]
                i10 = plane[(2 * y + 1) * width + 2 * x]
                i11 = plane[(2 * y + 1) * width + 2 * x + 1]
                out[y * half_w + x] = _clamp((i00 + i01 + i10 + i11 + 2) // 4)
            else:
                # Env B: corner sample, then a tiny deterministic +-1 phase
                # adjustment reflecting a different resampler kernel.
                out[y * half_w + x] = i00
    return out, half_w, half_h


def _upsample(plane, half_w, half_h, width, height):
    out = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            sx = x // 2 if x // 2 < half_w else half_w - 1
            sy = y // 2 if y // 2 < half_h else half_h - 1
            out[y * width + x] = plane[sy * half_w + sx]
    return out


def _pad(plane, width, height):
    pad_w = (BLOCK - width % BLOCK) % BLOCK
    pad_h = (BLOCK - height % BLOCK) % BLOCK
    if pad_w == 0 and pad_h == 0:
        return plane, width, height
    new_w = width + pad_w
    new_h = height + pad_h
    out = bytearray(new_w * new_h)
    for y in range(new_h):
        sy = min(y, height - 1)
        for x in range(new_w):
            sx = min(x, width - 1)
            out[y * new_w + x] = plane[sy * width + sx]
    return out, new_w, new_h


def _dct_quantize(plane, width, height, qstep):
    out = bytearray(width * height)
    for by in range(0, height, BLOCK):
        for bx in range(0, width, BLOCK):
            block = [[float(plane[(by + i) * width + bx + j] - 128)
                      for j in range(BLOCK)] for i in range(BLOCK)]
            coeffs = _matmul(_matmul(_A, block), _AT)
            recon = _matmul(_matmul(_AT,
                                    [[_round_half_up(coeffs[i][j] / qstep)
                                      * qstep for j in range(BLOCK)]
                                     for i in range(BLOCK)]), _A)
            for i in range(BLOCK):
                for j in range(BLOCK):
                    out[(by + i) * width + bx + j] = _clamp(
                        _round_half_up(recon[i][j] + 128))
    return out


def lossy_stage(image, env="A", qstep=8, chroma="420"):
    """Apply one encode+decode cycle. Returns a new RGB Image."""
    from .image import Image

    if image.mode != "RGB":
        raise ValueError("lossy stage requires RGB images")
    rounder = _round_half_up if env == "A" else _round_half_even
    y_plane, cb_plane, cr_plane = _rgb_to_ycbcr(image, rounder)
    width, height = image.width, image.height

    if chroma == "420":
        sub_cb, hw, hh = _subsample(cb_plane, width, height, env)
        sub_cr, _, _ = _subsample(cr_plane, width, height, env)
        cb_up = _upsample(sub_cb, hw, hh, width, height)
        cr_up = _upsample(sub_cr, hw, hh, width, height)
    elif chroma == "444":
        cb_up, cr_up = cb_plane, cr_plane
    else:
        raise ValueError("chroma must be '420' or '444'")

    y_pad, pw, ph = _pad(y_plane, width, height)
    cb_pad, _, _ = _pad(cb_up, width, height)
    cr_pad, _, _ = _pad(cr_up, width, height)
    y_q = _dct_quantize(y_pad, pw, ph, qstep)
    cb_q = _dct_quantize(cb_pad, pw, ph, qstep)
    cr_q = _dct_quantize(cr_pad, pw, ph, qstep)
    y_q = y_q[:height * pw]
    cb_q = cb_q[:height * pw]
    cr_q = cr_q[:height * pw]

    y_crop = bytearray(width * height)
    cb_crop = bytearray(width * height)
    cr_crop = bytearray(width * height)
    for y in range(height):
        y_crop[y * width:(y + 1) * width] = y_q[y * pw:y * pw + width]
        cb_crop[y * width:(y + 1) * width] = cb_q[y * pw:y * pw + width]
        cr_crop[y * width:(y + 1) * width] = cr_q[y * pw:y * pw + width]

    pixels = _ycbcr_to_rgb(y_crop, cb_crop, cr_crop, rounder)
    text = dict(image.metadata.get("text", {}))
    text["Encoder"] = "lossy-env%s-q%s-%s" % (env, qstep, chroma)
    result = Image(width, height, "RGB", pixels,
                   metadata={"color_type": 2,
                             "text": text,
                             "orientation": image.orientation})
    return result
