"""Differential regression tests: legacy call sites vs unified component.

For every original call site, the legacy implementation and the refactored
adapter must produce identical samples when driven by the same random
source. Where the legacy behaviour was a bug (silent truncation, dropped
quota remainders, zero-weight leakage, ZeroDivisionError), the divergence
is intentional and pinned down explicitly in DocumentedDivergenceTest.
"""

import random
import unittest
from unittest import mock

import sampling
from callsites import lottery as new_lottery
from callsites import promo as new_promo
from callsites import report as new_report
from legacy import lottery as legacy_lottery
from legacy import promo as legacy_promo
from legacy import report as legacy_report

SEEDS = (0, 1, 7, 42, 2024, 999983)


def _run_legacy_promo_with_seed(items, weights, k, seed):
    """legacy.promo news up random.Random() internally; patch the class so
    the instance it gets is deterministically seeded."""
    seeded = random.Random(seed)
    with mock.patch("random.Random", return_value=seeded):
        return legacy_promo.draw_by_weight(items, weights, k)


class LotteryDifferentialTest(unittest.TestCase):
    datasets = [
        (list(range(10)), 3),
        (list("abcdefgh"), 4),
        ([("u%d" % i) for i in range(25)], 7),
        ([1], 1),
        (list(range(6)), 6),
    ]

    def test_same_global_random_source_same_result(self):
        for records, k in self.datasets:
            for seed in SEEDS:
                random.seed(seed)
                expected = legacy_lottery.pick_winners(records, k)
                random.seed(seed)
                actual = new_lottery.pick_winners(
                    records, k, rng=sampling.ModuleSource()
                )
                self.assertEqual(
                    expected, actual, "lottery mismatch: k=%d seed=%d" % (k, seed)
                )


class PromoDifferentialTest(unittest.TestCase):
    datasets = [
        (["a", "b", "c"], [1, 2, 1], 5),
        (["x", "y", "z", "w"], [3, 1, 4, 8], 9),
        (list(range(6)), [1, 1, 1, 1, 1, 1], 12),
        (["only"], [2.5], 4),
        (["p", "q", "r"], [0, 3, 1], 8),  # leading zero weight
    ]

    def test_same_random_source_same_result(self):
        for items, weights, k in self.datasets:
            for seed in SEEDS:
                expected = _run_legacy_promo_with_seed(items, weights, k, seed)
                actual = new_promo.draw_by_weight(
                    items, weights, k, rng=sampling.SeededSource(seed)
                )
                self.assertEqual(
                    expected, actual, "promo mismatch: seed=%d" % seed
                )


class ReportDifferentialTest(unittest.TestCase):
    @staticmethod
    def _rows(sizes):
        rows = []
        for group, size in zip("ABCD", sizes):
            rows.extend({"group": group, "id": "%s-%d" % (group, i)}
                        for i in range(size))
        return rows

    def test_same_random_source_same_result_when_quotas_exact(self):
        # k * len(stratum) / total is an integer for every stratum, so the
        # legacy truncation and the unified largest-remainder agree.
        key = lambda row: row["group"]
        for sizes, k in [([8, 4], 6), ([10, 5, 5], 8), ([6, 6, 6, 6], 8)]:
            rows = self._rows(sizes)
            expected = legacy_report.stratified_sample(rows, key, k)
            actual = new_report.stratified_sample(
                rows, key, k, rng=sampling.SeededSource(legacy_report._SEED)
            )
            self.assertEqual(expected, actual, "report mismatch: k=%d" % k)
            self.assertEqual(len(actual), k)


class DocumentedDivergenceTest(unittest.TestCase):
    """Intentional behaviour fixes; each pins both sides of the change."""

    def test_lottery_oversample_now_raises(self):
        records = list(range(5))
        random.seed(0)
        self.assertEqual(len(legacy_lottery.pick_winners(records, 9)), 5)  # silent
        with self.assertRaises(sampling.SampleSizeError):
            new_lottery.pick_winners(records, 9, rng=sampling.SeededSource(0))

    def test_report_remainders_are_no_longer_dropped(self):
        key = lambda row: row["group"]
        rows = ReportDifferentialTest._rows([5, 5])
        legacy_out = legacy_report.stratified_sample(rows, key, 3)
        self.assertEqual(len(legacy_out), 2)  # 1.5 + 1.5 truncated to 1 + 1
        new_out = new_report.stratified_sample(
            rows, key, 3, rng=sampling.SeededSource(legacy_report._SEED)
        )
        self.assertEqual(len(new_out), 3)  # largest-remainder: 2 + 1

    def test_promo_zero_weight_item_never_selected(self):
        class ZeroSource:
            def random(self):
                return 0.0

            def randrange(self, n):
                return 0

        # Legacy: r=0.0 <= cumulative[0]=0.0 picks the zero-weight item.
        seeded = random.Random(0)
        seeded.random = lambda: 0.0
        with mock.patch("random.Random", return_value=seeded):
            self.assertEqual(legacy_promo.draw_by_weight(["z", "a"], [0, 1], 1), ["z"])
        # Unified: strict scan, zero-weight items are unreachable.
        out = new_promo.draw_by_weight(["z", "a"], [0, 1], 5, rng=ZeroSource())
        self.assertEqual(out, ["a"] * 5)

    def test_promo_all_zero_weights_raise_weight_error(self):
        with self.assertRaises(ZeroDivisionError):
            _run_legacy_promo_with_seed(["a"], [0], 1, 0)
        with self.assertRaises(sampling.WeightError):
            new_promo.draw_by_weight(["a"], [0], 1, rng=sampling.SeededSource(0))


if __name__ == "__main__":
    unittest.main()
