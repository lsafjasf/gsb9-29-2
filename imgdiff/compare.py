"""Per-pixel comparison, difference distribution and region localization."""

import heapq

# Difference histogram bucket upper bounds (inclusive), plus >255 sentinel.
BUCKETS = [0, 1, 2, 4, 8, 16, 32, 64, 128, 255]
BUCKET_LABELS = ["0", "1", "2", "3-4", "5-8", "9-16", "17-32",
                 "33-64", "65-128", "129-255"]


def _bucket_index(value):
    for idx, bound in enumerate(BUCKETS):
        if value <= bound:
            return idx
    return len(BUCKETS) - 1


def compare(reference, candidate, *, threshold=2, block_size=8,
            top_regions=5, top_pixels=8,
            mae_tol=1.0, max_tol=8, pct_tol=1.0):
    """Compare two images pixel by pixel.

    Returns a JSON-serializable report dict. ``threshold`` defines "over
    threshold" pixels by per-pixel max-channel absolute difference.
    """
    if reference.mode != candidate.mode:
        raise ValueError("mode mismatch: %s vs %s"
                         % (reference.mode, candidate.mode))
    width = reference.width
    height = reference.height
    channels = reference.channels
    if (width, height) != (candidate.width, candidate.height):
        raise ValueError("size mismatch: %dx%d vs %dx%d"
                         % (width, height, candidate.width, candidate.height))

    total = width * height
    ref = reference.pixels
    cand = candidate.pixels

    sum_abs = 0
    sum_sq = 0
    max_err = -1
    max_pos = None
    over_count = 0
    histogram = [0] * len(BUCKET_LABELS)
    channel_abs = [0] * channels

    # Min-heap of (score, position, values) keeps only the worst pixels.
    worst_heap = []

    for y in range(height):
        row_off = y * width * channels
        for x in range(width):
            off = row_off + x * channels
            pixel_max = 0
            ref_px = []
            cand_px = []
            for c in range(channels):
                a = ref[off + c]
                b = cand[off + c]
                d = abs(a - b)
                ref_px.append(a)
                cand_px.append(b)
                sum_abs += d
                sum_sq += d * d
                channel_abs[c] += d
                if d > pixel_max:
                    pixel_max = d
            if pixel_max > max_err:
                max_err = pixel_max
                max_pos = (x, y)
            if pixel_max > threshold:
                over_count += 1
            histogram[_bucket_index(pixel_max)] += 1
            if pixel_max == 0:
                continue
            entry = (pixel_max, x, y, tuple(ref_px), tuple(cand_px))
            if len(worst_heap) < top_pixels:
                heapq.heappush(worst_heap, entry)
            elif pixel_max > worst_heap[0][0]:
                heapq.heapreplace(worst_heap, entry)

    sample_count = total * channels
    mae = sum_abs / sample_count
    rmse = (sum_sq / sample_count) ** 0.5
    over_pct = over_count * 100.0 / total
    channel_mae = {_channel_name(channels, c): channel_abs[c] / total
                   for c in range(channels)}

    worst_pixels = [
        {"x": x, "y": y,
         "reference": list(rp), "candidate": list(cp),
         "max_channel_diff": score}
        for score, x, y, rp, cp in sorted(worst_heap, reverse=True)
    ]

    regions = _region_stats(ref, cand, width, height, channels, threshold,
                            block_size, top_regions)
    concentration = (regions[0]["mae"] / mae if regions and mae > 0 else None)
    metadata_report = _metadata_diff(reference, candidate)

    identical_pixels = total - sum(1 for i in range(total)
                                   if ref[i * channels:(i + 1) * channels]
                                   != cand[i * channels:(i + 1) * channels])

    stats = {
        "region_concentration": (round(concentration, 4)
                                 if concentration is not None else None),
        "pixels": total,
        "identical_pixels": identical_pixels,
        "different_pixels": total - identical_pixels,
        "mae": round(mae, 6),
        "rmse": round(rmse, 6),
        "max_abs_error": max(0, max_err),
        "max_error_position": ({"x": max_pos[0], "y": max_pos[1]}
                               if max_pos is not None else None),
        "threshold": threshold,
        "over_threshold_pixels": over_count,
        "over_threshold_pct": round(over_pct, 6),
        "channel_mae": {k: round(v, 6) for k, v in channel_mae.items()},
        "histogram": [{"bucket": label, "pixels": count,
                       "pct": round(count * 100.0 / total, 6)}
                      for label, count in zip(BUCKET_LABELS, histogram)],
    }

    metadata_only = (stats["different_pixels"] == 0
                     and not metadata_report["matches"])
    verdict, reason = _verdict(stats, metadata_report, metadata_only,
                               mae_tol, max_tol, pct_tol, concentration)

    return {
        "verdict": verdict,
        "reason": reason,
        "tolerances": {"mae": mae_tol, "max_abs": max_tol,
                       "over_pct": pct_tol, "threshold": threshold},
        "metadata": metadata_report,
        "stats": stats,
        "worst_regions": regions,
        "worst_pixels": worst_pixels,
    }


def _channel_name(channels, c):
    if channels == 1:
        return "L"
    if channels == 2:
        return ("L", "A")[c]
    return ("RGB" if channels == 3 else "RGBA")[c]


def _region_stats(ref, cand, width, height, channels, threshold,
                  block_size, top_regions):
    cols = (width + block_size - 1) // block_size
    rows = (height + block_size - 1) // block_size
    results = []
    for ry in range(rows):
        for rx in range(cols):
            x0 = rx * block_size
            y0 = ry * block_size
            x1 = min(width, x0 + block_size)
            y1 = min(height, y0 + block_size)
            cell = (x1 - x0) * (y1 - y0)
            abs_sum = 0
            local_max = 0
            local_max_pos = None
            over = 0
            ref_val = cand_val = None
            for y in range(y0, y1):
                row_off = y * width * channels
                for x in range(x0, x1):
                    off = row_off + x * channels
                    pixel_max = 0
                    for c in range(channels):
                        d = abs(ref[off + c] - cand[off + c])
                        abs_sum += d
                        if d > pixel_max:
                            pixel_max = d
                    if pixel_max > local_max:
                        local_max = pixel_max
                        local_max_pos = (x, y)
                        ref_val = list(ref[off:off + channels])
                        cand_val = list(cand[off:off + channels])
                    if pixel_max > threshold:
                        over += 1
            if local_max == 0:
                continue
            results.append({
                "bbox": {"x0": x0, "y0": y0, "x1": x1 - 1, "y1": y1 - 1},
                "mae": round(abs_sum / (cell * channels), 6),
                "max_abs_error": local_max,
                "over_threshold_pct": round(over * 100.0 / cell, 4),
                "max_point": {"x": local_max_pos[0], "y": local_max_pos[1]},
                "reference_at_max": ref_val,
                "candidate_at_max": cand_val,
            })
    results.sort(key=lambda r: (r["max_abs_error"], r["mae"]), reverse=True)
    return results[:top_regions]


def _metadata_diff(reference, candidate):
    ref_text = reference.metadata.get("text", {})
    cand_text = candidate.metadata.get("text", {})
    ref_ori = reference.orientation
    cand_ori = candidate.orientation
    text_keys = sorted(set(ref_text) | set(cand_text))
    text_changes = [
        {"key": key,
         "reference": ref_text.get(key),
         "candidate": cand_text.get(key)}
        for key in text_keys if ref_text.get(key) != cand_text.get(key)
    ]
    checks = {
        "width": reference.width == candidate.width,
        "height": reference.height == candidate.height,
        "color_mode": reference.mode == candidate.mode,
        "orientation": ref_ori == cand_ori,
        "text_chunks": not text_changes,
    }
    return {
        "matches": all(checks.values()),
        "checks": checks,
        "reference": {"width": reference.width, "height": reference.height,
                      "color_mode": reference.mode,
                      "orientation": ref_ori,
                      "text": dict(ref_text)},
        "candidate": {"width": candidate.width, "height": candidate.height,
                      "color_mode": candidate.mode,
                      "orientation": cand_ori,
                      "text": dict(cand_text)},
        "differences": text_changes,
    }


def _verdict(stats, metadata_report, metadata_only,
             mae_tol, max_tol, pct_tol, concentration=None):
    if stats["different_pixels"] == 0 and metadata_report["matches"]:
        return "identical", "pixels and metadata match exactly"
    if metadata_only:
        return "metadata_only", "pixel data identical; metadata differs"
    if stats["different_pixels"] == 1:
        return "single_pixel", "exactly one pixel differs"
    within = (stats["mae"] <= mae_tol
              and stats["max_abs_error"] <= max_tol
              and stats["over_threshold_pct"] <= pct_tol)
    if within:
        return ("within_tolerance",
                "diffuse small errors consistent with quantization noise")
    # Quantization noise: errors spread over the frame, individual values
    # stay moderate, and no small region concentrates the damage.
    # Algorithm differences: a compact region holds error far above the
    # frame-wide average (high max/MAE contrast + spatial concentration).
    diffuse = (stats["max_abs_error"] <= 4 * max_tol
               and stats["over_threshold_pct"] >= 25.0
               and (concentration is None or concentration < 2.5))
    if diffuse:
        return ("quantization",
                "diffuse moderate errors; consistent with codec "
                "quantization rather than an algorithm change")
    if stats["max_abs_error"] >= 3 * max_tol or stats["mae"] >= 3 * mae_tol \
            or (concentration is not None and concentration >= 3.0):
        return ("significant",
                "spatially concentrated or large error; likely an "
                "algorithm difference")
    return ("outside_tolerance",
            "errors exceed the configured tolerances")
