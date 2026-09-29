"""Generate reports/differential.md.

Runs every legacy call site and its refactored counterpart under the same
random source across many seeds/datasets and records whether the samples
are identical, plus the intentional divergences (legacy bugs that the
unified component fixes on purpose).
"""

import os
import random
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sampling
from callsites import lottery as new_lottery
from callsites import promo as new_promo
from callsites import report as new_report
from legacy import lottery as legacy_lottery
from legacy import promo as legacy_promo
from legacy import report as legacy_report

SEEDS = list(range(50))


def check_lottery():
    cases, mismatches = 0, 0
    datasets = [
        (list(range(10)), 3),
        (list("abcdefgh"), 4),
        (["u%d" % i for i in range(25)], 7),
        (list(range(6)), 6),
    ]
    for records, k in datasets:
        for seed in SEEDS:
            random.seed(seed)
            expected = legacy_lottery.pick_winners(records, k)
            random.seed(seed)
            actual = new_lottery.pick_winners(records, k, rng=sampling.ModuleSource())
            cases += 1
            mismatches += expected != actual
    return cases, mismatches


def check_promo():
    cases, mismatches = 0, 0
    datasets = [
        (["a", "b", "c"], [1, 2, 1], 5),
        (["x", "y", "z", "w"], [3, 1, 4, 8], 9),
        (list(range(6)), [1, 1, 1, 1, 1, 1], 12),
        (["p", "q", "r"], [0, 3, 1], 8),
    ]
    for items, weights, k in datasets:
        for seed in SEEDS:
            seeded = random.Random(seed)
            with mock.patch("random.Random", return_value=seeded):
                expected = legacy_promo.draw_by_weight(items, weights, k)
            actual = new_promo.draw_by_weight(
                items, weights, k, rng=sampling.SeededSource(seed)
            )
            cases += 1
            mismatches += expected != actual
    return cases, mismatches


def check_report():
    cases, mismatches = 0, 0
    key = lambda row: row["group"]
    for sizes, k in [([8, 4], 6), ([10, 5, 5], 8), ([6, 6, 6, 6], 8)]:
        rows = [
            {"group": g, "id": "%s-%d" % (g, i)}
            for g, size in zip("ABCD", sizes)
            for i in range(size)
        ]
        expected = legacy_report.stratified_sample(rows, key, k)
        actual = new_report.stratified_sample(
            rows, key, k, rng=sampling.SeededSource(legacy_report._SEED)
        )
        cases += 1
        mismatches += expected != actual
    return cases, mismatches


def divergences():
    lines = []
    records = list(range(5))
    random.seed(0)
    legacy_out = legacy_lottery.pick_winners(records, 9)
    try:
        new_lottery.pick_winners(records, 9, rng=sampling.SeededSource(0))
        new_out = "no error"
    except sampling.SampleSizeError as exc:
        new_out = "SampleSizeError: %s" % exc
    lines.append(
        ("lottery: k > population", "returns all %d records silently" % len(legacy_out),
         new_out)
    )
    rows = [{"group": g, "id": "%s-%d" % (g, i)} for g in "AB" for i in range(5)]
    key = lambda row: row["group"]
    legacy_out = legacy_report.stratified_sample(rows, key, 3)
    new_out = new_report.stratified_sample(
        rows, key, 3, rng=sampling.SeededSource(legacy_report._SEED)
    )
    lines.append(
        ("report: fractional quotas (1.5 + 1.5)",
         "truncates, returns %d of 3 rows" % len(legacy_out),
         "largest-remainder, returns %d of 3 rows" % len(new_out))
    )
    seeded = random.Random(0)
    seeded.random = lambda: 0.0
    with mock.patch("random.Random", return_value=seeded):
        legacy_out = legacy_promo.draw_by_weight(["z", "a"], [0, 1], 1)

    class ZeroSource:
        def random(self):
            return 0.0

        def randrange(self, n):
            return 0

    new_out = new_promo.draw_by_weight(["z", "a"], [0, 1], 1, rng=ZeroSource())
    lines.append(
        ("promo: zero-weight item, rng draws 0.0",
         "selects zero-weight item %r" % legacy_out,
         "never selects it: %r" % new_out)
    )
    try:
        with mock.patch("random.Random", return_value=random.Random(0)):
            legacy_promo.draw_by_weight(["a"], [0], 1)
        legacy_out = "no error"
    except ZeroDivisionError:
        legacy_out = "ZeroDivisionError"
    try:
        new_promo.draw_by_weight(["a"], [0], 1, rng=sampling.SeededSource(0))
        new_out = "no error"
    except sampling.WeightError as exc:
        new_out = "WeightError: %s" % exc
    lines.append(("promo: all-zero weights", legacy_out, new_out))
    return lines


def main():
    checks = [
        ("lottery.pick_winners", "UniformSampling", *check_lottery()),
        ("promo.draw_by_weight", "WeightedSampling(replace=True)", *check_promo()),
        ("report.stratified_sample", "StratifiedSampling", *check_report()),
    ]
    out = ["# Differential Report: legacy call sites vs unified component", ""]
    out.append("Same random source injected into both sides; samples must be identical.")
    out.append("")
    out.append("| Call site | Unified strategy | Cases | Mismatches | Verdict |")
    out.append("|---|---|---|---|---|")
    for name, strategy, cases, mismatches in checks:
        verdict = "PASS" if mismatches == 0 else "FAIL"
        out.append("| %s | %s | %d | %d | %s |" % (name, strategy, cases, mismatches, verdict))
    out.append("")
    out.append("## Intentional divergences (legacy bugs fixed by the unified component)")
    out.append("")
    out.append("| Scenario | Legacy behaviour | Unified behaviour |")
    out.append("|---|---|---|")
    for scenario, legacy, new in divergences():
        out.append("| %s | %s | %s |" % (scenario, legacy, new))
    out.append("")
    path = os.path.join(os.path.dirname(__file__), "..", "reports", "differential.md")
    with open(path, "w") as fh:
        fh.write("\n".join(out))
    print("wrote %s" % os.path.relpath(path))
    return any(m for _, _, _, m in checks)


if __name__ == "__main__":
    sys.exit(main())
