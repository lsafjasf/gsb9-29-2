"""Human-readable text rendering for JSON reports (ASCII only)."""


def render_compare(report):
    lines = []
    lines.append("VERDICT       : %s" % report["verdict"])
    lines.append("REASON        : %s" % report["reason"])
    tol = report["tolerances"]
    lines.append("TOLERANCES    : mae<=%s max_abs<=%s over_pct<=%s%% "
                 "(threshold >%d)"
                 % (tol["mae"], tol["max_abs"], tol["over_pct"],
                    tol["threshold"]))
    lines.append("")

    meta = report["metadata"]
    lines.append("[metadata]")
    for key, ok in meta["checks"].items():
        lines.append("  %-12s: %s" % (key, "OK" if ok else "DIFF"))
    for diff in meta["differences"]:
        lines.append("    text chunk %r: %r -> %r"
                     % (diff["key"], diff["reference"], diff["candidate"]))
    lines.append("")

    stats = report["stats"]
    lines.append("[pixel stats]")
    lines.append("  size                 : %d pixels" % stats["pixels"])
    lines.append("  identical / diff     : %d / %d"
                 % (stats["identical_pixels"], stats["different_pixels"]))
    lines.append("  MAE                  : %.4f" % stats["mae"])
    lines.append("  RMSE                 : %.4f" % stats["rmse"])
    lines.append("  max abs error        : %d at (x=%s, y=%s)"
                 % (stats["max_abs_error"],
                    stats["max_error_position"]["x"]
                    if stats["max_error_position"] else "-",
                    stats["max_error_position"]["y"]
                    if stats["max_error_position"] else "-"))
    lines.append("  over-threshold (>%d) : %d pixels (%.4f%%)"
                 % (stats["threshold"], stats["over_threshold_pixels"],
                    stats["over_threshold_pct"]))
    lines.append("  channel MAE          : "
                 + ", ".join("%s=%.4f" % (k, v)
                             for k, v in stats["channel_mae"].items()))
    lines.append("")
    lines.append("[difference histogram] (per-pixel max-channel delta)")
    for entry in stats["histogram"]:
        bar = "#" * int(round(entry["pct"] / 2.0))
        lines.append("  %8s: %9.4f%% %7d  %s"
                     % (entry["bucket"], entry["pct"],
                        entry["pixels"], bar))
    lines.append("")

    lines.append("[worst regions] (block-localized)")
    if not report["worst_regions"]:
        lines.append("  none")
    for idx, region in enumerate(report["worst_regions"], 1):
        box = region["bbox"]
        point = region["max_point"]
        lines.append("  #%d bbox=(x:%d-%d, y:%d-%d) MAE=%.4f max=%d "
                     "over=%.2f%%"
                     % (idx, box["x0"], box["x1"], box["y0"], box["y1"],
                        region["mae"], region["max_abs_error"],
                        region["over_threshold_pct"]))
        lines.append("      worst at (x=%d,y=%d): ref=%s cand=%s"
                     % (point["x"], point["y"],
                        region["reference_at_max"],
                        region["candidate_at_max"]))
    lines.append("")

    lines.append("[worst pixels]")
    if not report["worst_pixels"]:
        lines.append("  none")
    for entry in report["worst_pixels"]:
        lines.append("  (x=%d,y=%d) ref=%s cand=%s max_delta=%d"
                     % (entry["x"], entry["y"], entry["reference"],
                        entry["candidate"], entry["max_channel_diff"]))
    return "\n".join(lines)


def render_roundtrip(result):
    lines = []
    lines.append("ROUND-TRIP EVALUATION")
    lines.append("  mode=%s qstep=%d chroma=%s rounds=%d threshold>%d"
                 % (result["mode"], result["qstep"], result["chroma"],
                    result["rounds"], result["threshold"]))
    lim = result["limits"]
    lines.append("  limits: MAE<=%s max_abs<=%s over_pct<=%s%% "
                 "per-round MAE growth<=%s"
                 % (lim["mae"], lim["max_abs"], lim["over_pct"],
                    lim["delta_mae_per_round"]))
    lines.append("  RECOMMENDED MAX ROUND-TRIPS: %d"
                 % result["recommended_max_rounds"])
    if result["first_failure"]:
        lines.append("  first failure: round %d (%s)"
                     % (result["first_failure"]["round"],
                        ", ".join(result["first_failure"]["reasons"])))
    lines.append("")
    header = ("round  env  %8s  %8s  %6s  %10s  %9s  %s"
              % ("MAE", "deltaMAE", "maxErr", "overPx>%d"
                 % result["threshold"], "overPct", "ok"))
    lines.append(header)
    for rec in result["records"]:
        lines.append("%5d  %3s  %8.4f  %+8.4f  %6d  %10d  %8.4f%%  %s%s"
                     % (rec["round"], rec["env"], rec["mae"],
                        rec["delta_mae_vs_prev_round"],
                        rec["max_abs_error"], rec["over_threshold_pixels"],
                        rec["over_threshold_pct"],
                        "yes" if rec["acceptable"] else "NO",
                        ("  [" + ",".join(rec["breaches"]) + "]")
                        if rec["breaches"] else ""))
    lines.append("")
    last = result["records"][-1]
    if last["worst_region"]:
        region = last["worst_region"]
        box = region["bbox"]
        lines.append("worst region at round %d: bbox=(x:%d-%d,y:%d-%d) "
                     "MAE=%.4f max=%d ref=%s cand=%s"
                     % (last["round"], box["x0"], box["x1"],
                        box["y0"], box["y1"], region["mae"],
                        region["max_abs_error"],
                        region["reference_at_max"],
                        region["candidate_at_max"]))
    return "\n".join(lines)
