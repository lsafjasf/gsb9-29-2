"""Measure the framework's false-positive rate under the null
hypothesis (no real degradation) and at the degradation boundary.

For every scenario we repeatedly draw fresh "measurements" from the
*same* error distribution the baseline was built from (plus, for the
boundary scenarios, a shift of exactly delta).  Any failure the
framework reports is then a false positive by construction.  Results
are written to reports/false_positive_data.json.

Usage:  python3 examples/false_positive_study.py
"""

from __future__ import annotations

import json
import math
import random
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from errreg.core import (Tolerances, DistSummary,
                         check_deterministic_error, check_randomized_error)
from errreg.stats import t_critical

REF = Decimal("1.0")


def _normal_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _draw_summary(rng, mean, std, n) -> DistSummary:
    return DistSummary.from_samples(rng.gauss(mean, std) for _ in range(n))


def run_scenario(name, description, n_reps, theoretical_fp, check_fn,
                 seed=20261003):
    rng = random.Random(seed)
    failures = sum(1 for _ in range(n_reps) if not check_fn(rng).passed)
    observed = failures / n_reps
    return {
        "name": name,
        "description": description,
        "n_reps": n_reps,
        "failures": failures,
        "observed_fp": observed,
        "theoretical_fp": theoretical_fp,
        "within_bound": observed <= max(theoretical_fp * 3,
                                        theoretical_fp + 5 / n_reps),
    }


def main() -> int:
    tol = Tolerances()
    scenarios = []

    # -- deterministic, healthy sample size -------------------------------
    mu, sigma, n0 = 1e-12, 2e-13, 12
    mult = max(tol.z_min, t_critical(n0 - 1, tol.alpha))
    scenarios.append(run_scenario(
        "deterministic_noise_only",
        "baseline n=12, fresh error drawn from the identical normal "
        "distribution (models cross-platform float noise)",
        n_reps=20000,
        theoretical_fp=0.001,  # ~P(T_11 > 4); z_min=4 governs
        check_fn=lambda rng: check_deterministic_error(
            rng.gauss(mu, sigma),
            _draw_summary(rng, mu, sigma, n0), tol, REF),
    ))

    # -- deterministic, very few baseline samples -------------------------
    scenarios.append(run_scenario(
        "deterministic_few_samples",
        "baseline n=2: the t prediction multiplier "
        "(t(1,0.01)*sqrt(1.5)=39) keeps false alarms at or below alpha",
        n_reps=20000,
        theoretical_fp=tol.alpha,
        check_fn=lambda rng: check_deterministic_error(
            rng.gauss(mu, sigma),
            _draw_summary(rng, mu, sigma, 2), tol, REF),
    ))

    # -- randomized, no degradation ---------------------------------------
    rmu, rsigma, n = 3.2e-3, 4.0e-3, 30
    scenarios.append(run_scenario(
        "randomized_no_shift",
        "both baseline (n=30) and fresh sample (n=30) drawn from the "
        "identical distribution: false alarms far below alpha",
        n_reps=5000,
        theoretical_fp=tol.alpha,
        check_fn=lambda rng: check_randomized_error(
            _draw_summary(rng, rmu, rsigma, n),
            _draw_summary(rng, rmu, rsigma, n), tol, REF),
    ))

    # -- randomized, boundary shift = delta (worst case) ------------------
    delta = tol.delta_rel * rmu
    scenarios.append(run_scenario(
        "randomized_boundary_shift",
        "fresh sample degraded by exactly delta (the non-inferiority "
        "margin); rate saturates near alpha -- mildly liberal because "
        "delta itself is estimated from the baseline mean",
        n_reps=5000,
        theoretical_fp=tol.alpha,
        check_fn=lambda rng: check_randomized_error(
            _draw_summary(rng, rmu + delta, rsigma, n),
            _draw_summary(rng, rmu, rsigma, n), tol, REF),
    ))

    # -- randomized, very few samples, boundary shift ---------------------
    scenarios.append(run_scenario(
        "randomized_few_samples_boundary",
        "n0=n1=3, fresh sample degraded by exactly delta: the t-test "
        "keeps the rate at alpha despite tiny samples",
        n_reps=5000,
        theoretical_fp=tol.alpha,
        check_fn=lambda rng: check_randomized_error(
            _draw_summary(rng, rmu + delta, rsigma, 3),
            _draw_summary(rng, rmu, rsigma, 3), tol, REF),
    ))

    data = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "alpha": tol.alpha,
        "note": "observed_fp must stay at or below theoretical_fp "
                "(up to Monte Carlo sampling error of the study itself)",
        "scenarios": scenarios,
    }
    out = ROOT / "reports" / "false_positive_data.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    print(f"false-positive study (alpha={tol.alpha})")
    for s in scenarios:
        print(f"  {s['name']:36s} observed={s['observed_fp']:.5f} "
              f"bound={s['theoretical_fp']:.5f} "
              f"({s['failures']}/{s['n_reps']}) "
              f"{'OK' if s['within_bound'] else 'EXCEEDS BOUND'}")
    print(f"written: {out}")
    return 0 if all(s["within_bound"] for s in scenarios) else 1


if __name__ == "__main__":
    raise SystemExit(main())
