"""Command line interface:

  python -m imgdiff selftest
  python -m imgdiff compare ref.png cand.png [--json] [--threshold N] ...
  python -m imgdiff roundtrip input.png [--rounds N] [--mode ...] ...
  python -m imgdiff demo --outdir examples
"""

import argparse
import json
import os
import sys

from . import png, report as report_mod, selftest as selftest_mod
from .compare import compare
from .image import make_chart
from .roundtrip import evaluate

EXIT_OK = 0
EXIT_TOLERANCE = 2
EXIT_USAGE = 64


def _json_default(obj):
    if isinstance(obj, bytes):
        return obj.decode("latin-1")
    return str(obj)


def _emit(obj, as_json, text_renderer=None):
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True,
                         default=_json_default))
    else:
        print(text_renderer(obj))


def cmd_compare(args):
    ref = png.load(args.reference)
    cand = png.load(args.candidate)
    try:
        result = compare(ref, cand,
                         threshold=args.threshold,
                         block_size=args.block_size,
                         top_regions=args.top_regions,
                         top_pixels=args.top_pixels,
                         mae_tol=args.mae_tol,
                         max_tol=args.max_tol,
                         pct_tol=args.pct_tol)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_USAGE
    _emit(result, args.json, report_mod.render_compare)
    verdict = result["verdict"]
    if verdict in ("significant", "outside_tolerance"):
        return EXIT_TOLERANCE
    if verdict in ("quantization", "within_tolerance"):
        # Pass only when the measured error really fits the tolerances.
        stats = result["stats"]
        tol = result["tolerances"]
        if not (stats["mae"] <= tol["mae"]
                and stats["max_abs_error"] <= tol["max_abs"]
                and stats["over_threshold_pct"] <= tol["over_pct"]):
            return EXIT_TOLERANCE
    if args.strict_metadata and not result["metadata"]["matches"]:
        return EXIT_TOLERANCE
    return EXIT_OK


def cmd_roundtrip(args):
    ref = png.load(args.input) if args.input else make_chart()
    result = evaluate(ref, rounds=args.rounds, qstep=args.qstep,
                      mode=args.mode, chroma=args.chroma,
                      threshold=args.threshold,
                      mae_limit=args.mae_limit, max_limit=args.max_limit,
                      pct_limit=args.pct_limit,
                      delta_mae_limit=args.delta_mae_limit)
    final = result.pop("final_image")
    if args.save_final:
        png.dump(final, args.save_final)
    _emit(result, args.json, report_mod.render_roundtrip)
    if result["recommended_max_rounds"] == 0:
        return EXIT_TOLERANCE
    return EXIT_OK


def cmd_selftest(args):
    return EXIT_OK if selftest_mod.run() else EXIT_TOLERANCE


def cmd_demo(args):
    from .demo import run_demo
    run_demo(args.outdir)
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(
        prog="imgdiff",
        description="Image pipeline consistency checker (stdlib only)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_cmp = sub.add_parser("compare", help="compare two PNG images")
    p_cmp.add_argument("reference")
    p_cmp.add_argument("candidate")
    p_cmp.add_argument("--threshold", type=int, default=2,
                       help="over-threshold level in per-channel units "
                            "(default 2)")
    p_cmp.add_argument("--block-size", type=int, default=8)
    p_cmp.add_argument("--top-regions", type=int, default=5)
    p_cmp.add_argument("--top-pixels", type=int, default=8)
    p_cmp.add_argument("--mae-tol", type=float, default=1.0)
    p_cmp.add_argument("--max-tol", type=float, default=8.0)
    p_cmp.add_argument("--pct-tol", type=float, default=1.0,
                       help="max acceptable over-threshold pixel percent")
    p_cmp.add_argument("--strict-metadata", action="store_true",
                       help="return exit code 2 on any metadata difference")
    p_cmp.add_argument("--json", action="store_true")
    p_cmp.set_defaults(func=cmd_compare)

    p_rt = sub.add_parser("roundtrip",
                          help="evaluate cumulative drift over repeated "
                               "encode/decode cycles")
    p_rt.add_argument("input", nargs="?", default=None,
                      help="PNG input (defaults to built-in test chart)")
    p_rt.add_argument("--rounds", type=int, default=10)
    p_rt.add_argument("--qstep", type=int, default=8)
    p_rt.add_argument("--chroma", choices=("420", "444"), default="420")
    p_rt.add_argument("--mode", choices=("A", "B", "alternating"),
                      default="alternating")
    p_rt.add_argument("--threshold", type=int, default=2)
    p_rt.add_argument("--mae-limit", type=float, default=6.0)
    p_rt.add_argument("--max-limit", type=float, default=40.0)
    p_rt.add_argument("--pct-limit", type=float, default=100.0)
    p_rt.add_argument("--delta-mae-limit", type=float, default=1.0)
    p_rt.add_argument("--save-final")
    p_rt.add_argument("--json", action="store_true")
    p_rt.set_defaults(func=cmd_roundtrip)

    p_st = sub.add_parser("selftest", help="run built-in self tests")
    p_st.set_defaults(func=cmd_selftest)

    p_dm = sub.add_parser("demo", help="generate sample fixtures and reports")
    p_dm.add_argument("--outdir", default="examples")
    p_dm.set_defaults(func=cmd_demo)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
