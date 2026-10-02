#!/usr/bin/env python3
"""Estimate the false-positive (false-alarm) rate of the regression checks.

Three measurements are produced:

1. Model-based rate: parametric Monte Carlo from the fitted baseline
   distribution (Normal(mean, s)).  The theoretical per-case one-sided
   level of the Student-t interval is 0.5%, so a correctly built baseline
   should come out near 0.5%.
2. Empirical rate: run the *real* randomized example algorithm with many
   fresh seeds against the stored baseline and count how often a healthy
   algorithm is flagged.  A Wilson 95% interval is reported.
3. Deterministic repeatability: re-run the deterministic examples many
   times; healthy deterministic code must never alarm on the same platform.

Output: reports/false_positive_data.{json,txt}
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

from examples.problems import JacobiSolver, MonteCarloPi, Sqrt2Newton
from nrt import BaselineStore, Tolerances, check_case, measure, summarize, utc_now_stamp
from nrt.stats import degradation_limit

# Timing is environment-dependent and only a WARN; disable its slack when
# estimating the *error* false-positive rate so a loaded CI machine cannot
# pollute the count.
_FP_TOLS = Tolerances(time_factor=1e9)


def wilson_interval(k: int, n: int, z: float = 1.96):
    if n == 0:
        return 0.0, 0.0
    p = k / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom
    return center - half, center + half


def model_based_rate(baseline, current_n, trials, rng):
    """Re-sample baselines and new observations from the fitted Normal model."""
    summary = summarize(baseline.errors)
    alarms = 0
    for _ in range(trials):
        simulated_baseline = [rng.gauss(summary.mean, summary.stdev)
                              for _ in range(summary.n)]
        sim = summarize(simulated_baseline)
        if sim.stdev == 0.0:
            continue
        limit = degradation_limit(sim, current_n)
        current_mean = rng.gauss(summary.mean,
                                 summary.stdev / math.sqrt(current_n))
        if current_mean > limit:
            alarms += 1
    rate = alarms / trials
    return {"method": "model_based_normal", "trials": trials, "alarms": alarms,
            "rate": rate, "theoretical_one_sided_level": 0.005}


def empirical_rate(case, problem, trials, seed0=100_000):
    """Run the actual algorithm with fresh seeds; count healthy runs flagged."""
    alarms = 0
    for i in range(trials):
        errors, iters, times = measure(problem, 1, seed0=seed0 + i)
        result = check_case(case, errors, iters, times, defaults=_FP_TOLS)
        if result.status == "FAIL":
            alarms += 1
    lo, hi = wilson_interval(alarms, trials)
    return {"method": "empirical_real_algorithm", "trials": trials,
            "alarms": alarms, "rate": alarms / trials,
            "wilson95": [lo, hi]}


def deterministic_repeats(case, problem, trials):
    alarms = 0
    for _ in range(trials):
        errors, iters, times = measure(problem, 1)
        result = check_case(case, errors, iters, times, defaults=_FP_TOLS)
        if result.status == "FAIL":
            alarms += 1
    lo, hi = wilson_interval(alarms, trials)
    return {"method": "deterministic_repeat", "trials": trials,
            "alarms": alarms, "rate": alarms / trials,
            "wilson95": [lo, hi]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="baselines/baseline.json")
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--model-trials", type=int, default=20000)
    parser.add_argument("--empirical-trials", type=int, default=300)
    parser.add_argument("--deterministic-trials", type=int, default=50)
    args = parser.parse_args(argv)

    store = BaselineStore.load(args.baseline)
    rng = random.Random(20261003)
    data = {
        "generated_utc": utc_now_stamp(),
        "interval": "one-sided 99.5% Student-t prediction bound",
        "results": [],
    }

    for name, case in store.cases.items():
        if case.kind == "randomized":
            problem = MonteCarloPi(samples=20000)
            data["results"].append({
                "case": name,
                "model_based": model_based_rate(
                    case, current_n=8, trials=args.model_trials, rng=rng),
                "empirical": empirical_rate(
                    case, problem, trials=args.empirical_trials),
            })
        else:
            problem = (Sqrt2Newton() if name == "sqrt2_newton"
                       else JacobiSolver())
            data["results"].append({
                "case": name,
                "deterministic": deterministic_repeats(
                    case, problem, trials=args.deterministic_trials),
            })

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    out = report_dir / "false_positive_data.json"
    out.write_text(json.dumps(data, indent=2) + "\n")

    lines = [
        "False-positive measurement (healthy code vs stored baseline)",
        "=" * 64,
    ]
    for entry in data["results"]:
        lines.append(f"case: {entry['case']}")
        for measurement in entry.values():
            if not isinstance(measurement, dict):
                continue
            rate = measurement["rate"]
            extra = ""
            if "wilson95" in measurement:
                lo, hi = measurement["wilson95"]
                extra = f"  wilson95=[{lo:.4f}, {hi:.4f}]"
            lines.append(
                f"  {measurement['method']:<28} "
                f"alarms={measurement['alarms']:>5}/"
                f"{measurement['trials']:<6} rate={rate:.4%}{extra}"
            )
    lines.append("=" * 64)
    text = "\n".join(lines) + "\n"
    (report_dir / "false_positive_data.txt").write_text(text)
    print(text, end="")
    print(f"written: {out} and {report_dir / 'false_positive_data.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
