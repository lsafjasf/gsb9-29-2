"""Unbiasedness tests: observed frequencies vs theoretical probabilities.

Each check draws with a fixed seed (fully reproducible) and asserts that
every observed frequency lies within a 6-sigma binomial standard-error
band around the theoretical probability.
"""

import unittest

import sampling
from sampling.verify import (
    binomial_tolerance,
    inclusion_frequencies,
    position_frequencies,
)


class FrequencyCheck:
    @staticmethod
    def assert_close(testcase, observed, expected, trials, label):
        tol = binomial_tolerance(expected, trials)
        testcase.assertAlmostEqual(
            observed, expected, delta=tol,
            msg="%s: observed %.5f vs expected %.5f (tol %.5f, n=%d)"
            % (label, observed, expected, tol, trials),
        )


class UniformUnbiasednessTest(unittest.TestCase):
    def test_each_item_included_with_probability_k_over_n(self):
        items = list(range(10))
        k, trials = 4, 60_000
        rng = sampling.SeededSource(20260930)
        strategy = sampling.UniformSampling()
        draws = [strategy.sample(items, k, rng=rng) for _ in range(trials)]
        observed = inclusion_frequencies(draws, items)
        for item in items:
            FrequencyCheck.assert_close(
                self, observed[item], k / len(items), trials,
                "uniform item %d" % item,
            )


class WeightedUnbiasednessTest(unittest.TestCase):
    def test_with_replacement_matches_normalized_weights(self):
        items = ["a", "b", "c"]
        weights = [1, 2, 1]
        total = float(sum(weights))
        k, trials = 3, 40_000
        rng = sampling.SeededSource(20260930)
        strategy = sampling.WeightedSampling(weights, replace=True)
        draws = [strategy.sample(items, k, rng=rng) for _ in range(trials)]
        observed = position_frequencies(draws, items)
        for item, w in zip(items, weights):
            FrequencyCheck.assert_close(
                self, observed[item], w / total, trials * k,
                "weighted(replace) item %s" % item,
            )

    def test_without_replacement_first_pick_matches_normalized_weights(self):
        # A-Res property: the first selected item (largest key) is drawn
        # with probability exactly w_i / sum(w).
        items = ["a", "b", "c"]
        weights = [3, 1, 4]
        total = float(sum(weights))
        trials = 40_000
        rng = sampling.SeededSource(20260930)
        strategy = sampling.WeightedSampling(weights, replace=False)
        firsts = [[strategy.sample(items, 2, rng=rng)[0]] for _ in range(trials)]
        observed = inclusion_frequencies(firsts, items)
        for item, w in zip(items, weights):
            FrequencyCheck.assert_close(
                self, observed[item], w / total, trials,
                "weighted(no-replace) first pick %s" % item,
            )


class StratifiedUnbiasednessTest(unittest.TestCase):
    def test_within_stratum_inclusion_probability(self):
        rows = [{"g": "A", "id": i} for i in range(10)]
        rows += [{"g": "B", "id": 100 + i} for i in range(5)]
        k, trials = 6, 40_000  # quotas: A -> 4/10, B -> 2/5
        rng = sampling.SeededSource(20260930)
        strategy = sampling.StratifiedSampling(key=lambda r: r["g"])
        draws = [strategy.sample(rows, k, rng=rng) for _ in range(trials)]
        flat_items = [row["id"] for row in rows]
        observed = inclusion_frequencies(
            [[x["id"] for x in d] for d in draws], flat_items
        )
        expected = {"A": 4 / 10, "B": 2 / 5}
        for row in rows:
            FrequencyCheck.assert_close(
                self, observed[row["id"]], expected[row["g"]], trials,
                "stratified row %s" % row["id"],
            )


class ReservoirUnbiasednessTest(unittest.TestCase):
    def test_streaming_uniform_inclusion(self):
        n, k, trials = 50, 5, 20_000
        rng = sampling.SeededSource(20260930)
        draws = [
            sampling.reservoir_sample(iter(range(n)), k, rng=rng)
            for _ in range(trials)
        ]
        observed = inclusion_frequencies(draws, list(range(n)))
        for item in range(n):
            FrequencyCheck.assert_close(
                self, observed[item], k / n, trials,
                "reservoir item %d" % item,
            )


if __name__ == "__main__":
    unittest.main()
