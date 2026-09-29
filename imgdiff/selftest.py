"""Built-in self tests: four required scenarios plus codec sanity checks."""

import sys

from . import png
from .compare import compare
from .image import Image, make_chart
from .roundtrip import evaluate


class AssertError(AssertionError):
    pass


def _check(condition, message):
    if not condition:
        raise AssertError(message)


def _build_fixtures():
    base = make_chart(32, 32, seed=11)

    identical = base.copy()

    metadata_only = base.copy()
    metadata_only = metadata_only.with_orientation(6)

    single_pixel = base.copy()
    px = single_pixel.get_pixel(7, 9)
    single_pixel.set_pixel(7, 9,
                           tuple(0 if v > 127 else 255 for v in px))

    significant = base.copy()
    buf = bytearray(significant.pixels)
    for y in range(8, 24):
        for x in range(8, 24):
            off = (y * 32 + x) * 3
            buf[off] = min(255, buf[off] + 120)
            buf[off + 1] = max(0, buf[off + 1] - 120)
    significant = Image(32, 32, "RGB", bytes(buf),
                        metadata=dict(significant.metadata))
    return base, identical, metadata_only, single_pixel, significant


def run(verbose=True):
    failures = []
    checks = [
        "case: identical",
        "case: metadata only",
        "case: single pixel",
        "case: significant difference",
        "png lossless round trips (L/RGB/RGBA)",
        "lossless PNG re-encode does not drift",
        "lossy cumulative drift is non-decreasing (alternating)",
        "round-trip recommendation reported",
        "size mismatch is rejected",
    ]

    def section(name):
        if verbose:
            print("  - %s ... " % name, end="")
            sys.stdout.flush()

    def ok():
        if verbose:
            print("PASS")

    (base, identical, metadata_only, single_pixel,
     significant) = _build_fixtures()

    try:
        section(checks[0])
        report = compare(base, identical)
        _check(report["verdict"] == "identical", report["verdict"])
        _check(report["stats"]["mae"] == 0.0, "nonzero MAE")
        _check(report["stats"]["max_abs_error"] == 0, "nonzero max")
        _check(report["metadata"]["matches"], "metadata mismatch")
        _check(report["worst_regions"] == [], "spurious regions")
        ok()
    except Exception as exc:  # noqa: BLE001 - report and continue
        failures.append((checks[0], exc))

    try:
        section(checks[1])
        report = compare(base, metadata_only)
        _check(report["verdict"] == "metadata_only", report["verdict"])
        _check(report["stats"]["different_pixels"] == 0, "pixels differ")
        _check(not report["metadata"]["checks"]["orientation"],
               "orientation not flagged")
        _check(report["metadata"]["reference"]["orientation"] == 1
               and report["metadata"]["candidate"]["orientation"] == 6,
               "orientation values wrong")
        ok()
    except Exception as exc:  # noqa: BLE001
        failures.append((checks[1], exc))

    try:
        section(checks[2])
        report = compare(base, single_pixel)
        _check(report["verdict"] == "single_pixel", report["verdict"])
        _check(report["stats"]["different_pixels"] == 1,
                "expected exactly 1 diff pixel, got %d"
                % report["stats"]["different_pixels"])
        pos = report["stats"]["max_error_position"]
        _check(pos == {"x": 7, "y": 9}, "wrong max position %r" % pos)
        _check(len(report["worst_pixels"]) == 1, "worst pixel not isolated")
        ok()
    except Exception as exc:  # noqa: BLE001
        failures.append((checks[2], exc))

    try:
        section(checks[3])
        report = compare(base, significant,
                         mae_tol=1.0, max_tol=8, pct_tol=1.0)
        _check(report["verdict"] == "significant", report["verdict"])
        boxes = [r["bbox"] for r in report["worst_regions"]]
        inside = [b for b in boxes
                  if b["x0"] >= 8 and b["x1"] <= 23
                  and b["y0"] >= 8 and b["y1"] <= 23]
        _check(inside, "no worst region inside changed block: %r" % boxes)
        region = report["worst_regions"][0]
        box = region["bbox"]
        _check(report["stats"]["max_abs_error"] >= 120,
               "max error too small")
        _check(region["reference_at_max"] != region["candidate_at_max"],
               "missing sample values")
        ok()
    except Exception as exc:  # noqa: BLE001
        failures.append((checks[3], exc))

    try:
        section(checks[4])
        for mode, channels in (("L", 1), ("RGB", 3), ("RGBA", 4)):
            payload = bytes((i * 37 + channels) % 256
                            for i in range(32 * 24 * channels))
            img = Image(32, 24, mode, payload,
                        metadata={"color_type": {"L": 0, "RGB": 2,
                                                 "RGBA": 6}[mode],
                                  "text": {}, "orientation": 1})
            decoded = png.loads(png.dumps(img))
            _check(decoded.pixels == img.pixels,
                   "%s pixels changed through PNG" % mode)
            _check(decoded.width == 32 and decoded.height == 24,
                   "%s dimensions changed" % mode)
        ok()
    except Exception as exc:  # noqa: BLE001
        failures.append((checks[4], exc))

    try:
        section(checks[5])
        once = png.loads(png.dumps(base))
        twice = png.loads(png.dumps(once))
        _check(twice.pixels == base.pixels, "PNG is not idempotent")
        ok()
    except Exception as exc:  # noqa: BLE001
        failures.append((checks[5], exc))

    try:
        section(checks[6])
        result = evaluate(base, rounds=6, qstep=8, mode="alternating",
                          mae_limit=100, max_limit=255, pct_limit=100,
                          delta_mae_limit=100)
        # Alternating A/B must not decrease drift vs round 2 over the run.
        maes = [rec["mae"] for rec in result["records"]]
        _check(maes[-1] >= maes[1] - 0.01,
               "cumulative drift unexpectedly decreased: %r" % maes)
        _check(all(rec["env"] in ("A", "B") for rec in result["records"]),
               "bad env labels")
        _check(all("mae" in rec and "worst_region" in rec
                   for rec in result["records"]),
               "records missing fields")
        ok()
    except Exception as exc:  # noqa: BLE001
        failures.append((checks[6], exc))

    try:
        section(checks[7])
        result = evaluate(base, rounds=4, qstep=8, mode="alternating",
                          mae_limit=5.2, max_limit=40, pct_limit=100)
        _check(isinstance(result["recommended_max_rounds"], int),
                "recommendation not an int")
        _check(0 <= result["recommended_max_rounds"] <= 4,
               "recommendation out of range")
        _check(result["first_failure"] is not None
               or result["recommended_max_rounds"] == 4,
               "failure bookkeeping wrong")
        ok()
    except Exception as exc:  # noqa: BLE001
        failures.append((checks[7], exc))

    try:
        section(checks[8])
        wrong_size = Image(31, 32, "RGB", bytes(31 * 32 * 3))
        raised = False
        try:
            compare(base, wrong_size)
        except ValueError:
            raised = True
        _check(raised, "size mismatch not rejected")
        ok()
    except Exception as exc:  # noqa: BLE001
        failures.append((checks[8], exc))

    if verbose:
        print("")
        if failures:
            print("SELF TEST: %d/%d FAILED" % (len(failures), len(checks)))
            for name, exc in failures:
                print("  FAIL %s: %s" % (name, exc))
        else:
            print("SELF TEST: %d/%d PASSED" % (len(checks), len(checks)))
    return not failures
