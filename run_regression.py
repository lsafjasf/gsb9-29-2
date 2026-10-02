#!/usr/bin/env python3
"""Error-regression runner: maintain baselines, check for degradation.

Usage:
  python3 run_regression.py --update                 # (re)build baselines/baseline.json
  python3 run_regression.py                          # check against the baseline
  python3 run_regression.py --simulate-degradation   # demo: inject a regression

Exit code is 0 when every case passes (WARN allowed), 1 on any FAIL.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from examples.problems import (
    DegradedProblem,
    JacobiSolver,
    MonteCarloPi,
    Sqrt2Newton,
)
from nrt import BaselineStore, CaseBaseline, check_case, measure, utc_now_stamp
from nrt.report import render_text, to_json_dict

BASELINE_RUNS = {"deterministic": 3, "randomized": 30}
CHECK_RUNS = {"deterministic": 1, "randomized": 8}
BASELINE_SEED = 10_000
CHECK_SEED = 20_000


def build_problems(simulate_degradation: bool = False):
    problems = [Sqrt2Newton(), JacobiSolver(), MonteCarloPi(samples=20000)]
    if simulate_degradation:
        problems = [
            DegradedProblem(p) if p.name == "jacobi_solver" else p
            for p in problems
        ]
    return problems


def cmd_update(args) -> int:
    cases = {}
    for problem in build_problems():
        runs = BASELINE_RUNS[problem.kind]
        errors, iters, times = measure(problem, runs, seed0=BASELINE_SEED)
        cases[problem.name] = CaseBaseline(
            name=problem.name,
            kind=problem.kind,
            errors=errors,
            iterations=iters,
            times_ms=times,
            note=f"baseline runs={runs} seed0={BASELINE_SEED}",
        )
    store = BaselineStore(cases=cases, created_utc=utc_now_stamp())
    store.save(args.baseline)
    print(f"wrote {args.baseline} ({len(cases)} cases)")
    return 0


def cmd_check(args) -> int:
    store = BaselineStore.load(args.baseline)
    problems = {p.name: p for p in build_problems(args.simulate_degradation)}
    results = []
    for name, case in store.cases.items():
        problem = problems[name]
        runs = CHECK_RUNS[case.kind]
        errors, iters, times = measure(problem, runs, seed0=CHECK_SEED)
        results.append(check_case(case, errors, iters, times))

    meta = {
        "generated_utc": utc_now_stamp(),
        "baseline": str(args.baseline),
        "simulated_degradation": args.simulate_degradation,
    }
    text = render_text(results, meta)
    print(text, end="")

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    suffix = "degraded_sample" if args.simulate_degradation else "latest"
    (report_dir / f"report_{suffix}.txt").write_text(text)
    (report_dir / f"report_{suffix}.json").write_text(
        json.dumps(to_json_dict(results, meta), indent=2) + "\n"
    )
    print(f"reports written to {report_dir}/report_{suffix}.{{txt,json}}")
    return 1 if any(r.status == "FAIL" for r in results) else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="baselines/baseline.json",
                        help="path to the baseline store (JSON)")
    parser.add_argument("--report-dir", default="reports",
                        help="directory for generated reports")
    parser.add_argument("--update", action="store_true",
                        help="rebuild the baseline from the current code")
    parser.add_argument("--simulate-degradation", action="store_true",
                        help="inject an artificial regression (demo)")
    args = parser.parse_args(argv)
    if args.update:
        return cmd_update(args)
    return cmd_check(args)


if __name__ == "__main__":
    sys.exit(main())
