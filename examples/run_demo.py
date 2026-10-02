"""Build baselines, run all cases (including degraded variants),
and write the sample regression report under reports/.

Usage:
    python3 examples/run_demo.py            # rebuild baselines + report
    python3 examples/run_demo.py --no-build # reuse existing baselines
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from errreg.runner import Runner
from errreg.report import write_reports, print_console
from examples.problems import good_cases, degraded_cases

BASELINE_DIR = ROOT / "baselines"
REPORT_DIR = ROOT / "reports"


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    runner = Runner(str(BASELINE_DIR))

    if "--no-build" not in argv:
        print("Building baselines ...")
        for case in good_cases():
            baseline = runner.build_baseline(case)
            print(f"  baseline {case.name}: abs_err "
                  f"mean={baseline.abs_error.mean:.3e} "
                  f"std={baseline.abs_error.std:.2e} "
                  f"n={baseline.abs_error.n} "
                  f"iters={baseline.iterations.mean:.1f}")

    cases = good_cases() + degraded_cases()
    print("\nRunning regression checks ...")
    results = [runner.check(case) for case in cases]
    rc = print_console(results)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    paths = write_reports(results, REPORT_DIR, label="sample_report")
    print(f"\nReport written:\n  {paths['markdown']}\n  {paths['json']}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
