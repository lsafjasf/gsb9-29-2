"""Generate the sample fixtures, comparison reports and round-trip data."""

import json
import os

from . import png, report as report_mod
from .compare import compare
from .image import Image, make_chart
from .roundtrip import evaluate


def _write(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _save_compare(outdir, name, reference, candidate, **kwargs):
    result = compare(reference, candidate, **kwargs)
    _write(os.path.join(outdir, "%s.txt" % name),
           report_mod.render_compare(result) + "\n")
    _write(os.path.join(outdir, "%s.json" % name),
            json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def run_demo(outdir):
    os.makedirs(outdir, exist_ok=True)
    base = make_chart(64, 64, seed=7)
    png.dump(base, os.path.join(outdir, "base.png"))

    # 1. Exact match (re-encoded losslessly).
    exact = png.loads(png.dumps(base))
    png.dump(exact, os.path.join(outdir, "identical.png"))
    _save_compare(outdir, "compare_identical", base, exact)

    # 2. Metadata only: same pixels, EXIF-style orientation flipped.
    oriented = base.with_orientation(6)
    png.dump(oriented, os.path.join(outdir, "metadata_only.png"))
    _save_compare(outdir, "compare_metadata_only", base, oriented)

    # 3. Single pixel: one channel spike at a known coordinate.
    one_pixel = base.copy()
    one_pixel.set_pixel(37, 21, (255, 0, 128))
    png.dump(one_pixel, os.path.join(outdir, "single_pixel.png"))
    _save_compare(outdir, "compare_single_pixel", base, one_pixel)

    # 4. Significant difference: a structured 16x16 region is recolored.
    significant = base.copy()
    buf = bytearray(significant.pixels)
    for y in range(24, 40):
        for x in range(24, 40):
            off = (y * 64 + x) * 3
            buf[off] = min(255, buf[off] + 150)
            buf[off + 1] = max(0, buf[off + 1] - 150)
            buf[off + 2] = max(0, buf[off + 2] - 90)
    significant = Image(64, 64, "RGB", bytes(buf),
                        metadata=dict(significant.metadata))
    png.dump(significant, os.path.join(outdir, "significant.png"))
    _save_compare(outdir, "compare_significant", base, significant)

    # 5. Quantization noise: one env-A lossy cycle (looks visually identical).
    from . import codec
    quantized = codec.lossy_stage(base, env="A", qstep=8, chroma="420")
    png.dump(quantized, os.path.join(outdir, "quantized_envA.png"))
    _save_compare(outdir, "compare_quantized", base, quantized)

    # Round-trip tables for the three deployment scenarios.
    summary = {}
    scenarios = [
        ("roundtrip_envA", "A", 8, "420", 2, 6.0, 40, 100.0, 1.0),
        ("roundtrip_envB", "B", 8, "420", 2, 6.0, 40, 100.0, 1.0),
        ("roundtrip_alternating", "alternating", 8, "420", 2, 6.0, 40,
         100.0, 1.0),
    ]
    for (name, mode, qstep, chroma, threshold, mae_limit, max_limit,
         pct_limit, delta_limit) in scenarios:
        result = evaluate(base, rounds=10, qstep=qstep, mode=mode,
                          chroma=chroma, threshold=threshold,
                          mae_limit=mae_limit, max_limit=max_limit,
                          pct_limit=pct_limit,
                          delta_mae_limit=delta_limit)
        result.pop("final_image", None)
        _write(os.path.join(outdir, "%s.txt" % name),
               report_mod.render_roundtrip(result) + "\n")
        _write(os.path.join(outdir, "%s.json" % name),
               json.dumps(result, indent=2, sort_keys=True) + "\n")
        summary[name] = {
            "mode": mode,
            "recommended_max_rounds": result["recommended_max_rounds"],
            "first_failure": result["first_failure"],
            "mae_by_round": [r["mae"] for r in result["records"]],
            "max_by_round": [r["max_abs_error"] for r in result["records"]],
        }
    _write(os.path.join(outdir, "roundtrip_summary.json"),
           json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print("demo artifacts written to %s" % os.path.abspath(outdir))
