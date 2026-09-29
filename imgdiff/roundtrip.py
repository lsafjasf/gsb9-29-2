"""Cumulative-drift evaluation across repeated encode/decode cycles."""

from . import codec
from .compare import compare


def evaluate(reference, *, rounds=8, qstep=8, mode="alternating",
             chroma="420", threshold=2, mae_limit=6.0, max_limit=40,
             pct_limit=100.0, delta_mae_limit=1.0):
    """Repeatedly encode+decode and measure drift against the reference.

    mode:
      "A"           - always environment A (round half up, box chroma)
      "B"           - always environment B (round half even, corner chroma)
      "alternating" - A, B, A, B ... models data bouncing between two
                      slightly different deployments

    A round is "acceptable" while MAE, max error and over-threshold ratio
    stay within limits. ``recommended_max_rounds`` is the longest prefix of
    acceptable rounds; the first failure reason is recorded.
    """
    current = reference
    records = []
    recommended = rounds
    first_failure = None

    for index in range(1, rounds + 1):
        if mode == "A":
            env = "A"
        elif mode == "B":
            env = "B"
        else:
            env = "A" if index % 2 == 1 else "B"
        current = codec.lossy_stage(current, env=env,
                                         qstep=qstep, chroma=chroma)
        report = compare(reference, current, threshold=threshold,
                         top_regions=3, top_pixels=3)
        stats = report["stats"]
        prev_mae = records[-1]["mae"] if records else 0.0
        delta = round(stats["mae"] - prev_mae, 6)

        breaches = []
        if stats["mae"] > mae_limit:
            breaches.append("mae>%.3f" % mae_limit)
        if stats["max_abs_error"] > max_limit:
            breaches.append("max_abs>%d" % max_limit)
        if stats["over_threshold_pct"] > pct_limit:
            breaches.append("over_pct>%.3f" % pct_limit)
        if index >= 2 and delta > delta_mae_limit:
            breaches.append("per_round_mae_growth>%.3f" % delta_mae_limit)

        acceptable = not breaches
        if not acceptable and first_failure is None:
            first_failure = {"round": index, "reasons": breaches}
            recommended = index - 1

        records.append({
            "round": index,
            "env": env,
            "mae": stats["mae"],
            "rmse": stats["rmse"],
            "max_abs_error": stats["max_abs_error"],
            "over_threshold_pixels": stats["over_threshold_pixels"],
            "over_threshold_pct": stats["over_threshold_pct"],
            "different_pixels": stats["different_pixels"],
            "delta_mae_vs_prev_round": delta,
            "acceptable": acceptable,
            "breaches": breaches,
            "worst_region": report["worst_regions"][0]
            if report["worst_regions"] else None,
        })

    if first_failure is None:
        recommended = rounds
    return {
        "mode": mode,
        "rounds": rounds,
        "qstep": qstep,
        "chroma": chroma,
        "threshold": threshold,
        "limits": {"mae": mae_limit, "max_abs": max_limit,
                   "over_pct": pct_limit,
                   "delta_mae_per_round": delta_mae_limit},
        "recommended_max_rounds": recommended,
        "first_failure": first_failure,
        "records": records,
        "final_image": current,
    }
