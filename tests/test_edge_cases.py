"""Edge cases: zero weights, oversampling, streaming, dead random sources."""

import unittest

import sampling
from sampling import (
    RandomSourceUnavailableError,
    SampleSizeError,
    SeededSource,
    WeightError,
)


class ZeroWeightTest(unittest.TestCase):
    def test_zero_weight_items_are_never_selected_with_replacement(self):
        strategy = sampling.WeightedSampling([0, 1, 0, 2, 0], replace=True)
        out = strategy.sample(list("abcde"), 500, rng=SeededSource(1))
        self.assertTrue(set(out) <= {"b", "d"})

    def test_zero_weight_items_are_never_selected_without_replacement(self):
        strategy = sampling.WeightedSampling([0, 1, 0, 2, 0], replace=False)
        out = strategy.sample(list("abcde"), 2, rng=SeededSource(1))
        self.assertEqual(sorted(out), ["b", "d"])

    def test_zero_weight_survives_degenerate_zero_draw(self):
        class ZeroSource:
            def random(self):
                return 0.0

            def randrange(self, n):
                return 0

        strategy = sampling.WeightedSampling([0, 0, 1], replace=True)
        self.assertEqual(
            strategy.sample(list("abc"), 3, rng=ZeroSource()), ["c", "c", "c"]
        )

    def test_all_zero_weights_rejected(self):
        with self.assertRaises(WeightError):
            sampling.WeightedSampling([0, 0, 0])

    def test_negative_weight_rejected(self):
        with self.assertRaises(WeightError):
            sampling.WeightedSampling([1, -0.5, 2])

    def test_weight_length_mismatch_rejected(self):
        strategy = sampling.WeightedSampling([1, 2])
        with self.assertRaises(WeightError):
            strategy.sample(list("abc"), 1, rng=SeededSource(1))


class OversampleTest(unittest.TestCase):
    def test_uniform_raises_when_k_exceeds_population(self):
        with self.assertRaises(SampleSizeError):
            sampling.UniformSampling().sample([1, 2, 3], 4, rng=SeededSource(1))

    def test_weighted_without_replacement_raises(self):
        strategy = sampling.WeightedSampling([1, 1], replace=False)
        with self.assertRaises(SampleSizeError):
            strategy.sample([1, 2], 3, rng=SeededSource(1))

    def test_weighted_without_replacement_raises_when_positives_too_few(self):
        strategy = sampling.WeightedSampling([1, 0, 0], replace=False)
        with self.assertRaises(SampleSizeError):
            strategy.sample([1, 2, 3], 2, rng=SeededSource(1))

    def test_weighted_with_replacement_allows_oversample(self):
        strategy = sampling.WeightedSampling([1, 1], replace=True)
        self.assertEqual(
            len(strategy.sample([1, 2], 10, rng=SeededSource(1))), 10
        )

    def test_stratified_raises_when_k_exceeds_population(self):
        with self.assertRaises(SampleSizeError):
            sampling.StratifiedSampling(key=lambda x: x % 2).sample(
                [1, 2, 3], 4, rng=SeededSource(1)
            )

    def test_k_equal_to_population_is_allowed(self):
        out = sampling.UniformSampling().sample([1, 2, 3], 3, rng=SeededSource(1))
        self.assertEqual(sorted(out), [1, 2, 3])

    def test_k_zero_returns_empty(self):
        rng = SeededSource(1)
        self.assertEqual(sampling.UniformSampling().sample([1, 2], 0, rng=rng), [])
        self.assertEqual(
            sampling.WeightedSampling([1, 1]).sample([1, 2], 0, rng=rng), []
        )
        self.assertEqual(
            sampling.StratifiedSampling(key=lambda x: x).sample([1, 2], 0, rng=rng),
            [],
        )

    def test_negative_k_rejected(self):
        with self.assertRaises(ValueError):
            sampling.UniformSampling().sample([1, 2], -1, rng=SeededSource(1))


class StreamingTest(unittest.TestCase):
    def test_reservoir_accepts_a_generator(self):
        out = sampling.reservoir_sample(
            (x for x in range(100)), 5, rng=SeededSource(7)
        )
        self.assertEqual(len(out), 5)
        self.assertEqual(len(set(out)), 5)
        self.assertTrue(set(out) <= set(range(100)))

    def test_reservoir_is_reproducible_per_seed(self):
        a = sampling.reservoir_sample(iter(range(100)), 5, rng=SeededSource(7))
        b = sampling.reservoir_sample(iter(range(100)), 5, rng=SeededSource(7))
        self.assertEqual(a, b)

    def test_reservoir_raises_when_stream_shorter_than_k(self):
        with self.assertRaises(SampleSizeError):
            sampling.reservoir_sample(iter([1, 2]), 3, rng=SeededSource(1))

    def test_weighted_reservoir_skips_zero_weights(self):
        stream = iter([("a", 0), ("b", 1), ("c", 0), ("d", 2)])
        out = sampling.weighted_reservoir_sample(stream, 2, rng=SeededSource(3))
        self.assertEqual(sorted(out), ["b", "d"])

    def test_weighted_reservoir_rejects_negative_weight(self):
        with self.assertRaises(WeightError):
            sampling.weighted_reservoir_sample(
                iter([("a", -1)]), 1, rng=SeededSource(1)
            )


class RandomSourceUnavailableTest(unittest.TestCase):
    strategies = (
        lambda rng: sampling.UniformSampling().sample([1, 2, 3], 2, rng=rng),
        lambda rng: sampling.WeightedSampling([1, 2, 3]).sample([1, 2, 3], 2, rng=rng),
        lambda rng: sampling.StratifiedSampling(key=lambda x: x % 2).sample(
            [1, 2, 3], 2, rng=rng
        ),
        lambda rng: sampling.reservoir_sample(iter([1, 2, 3]), 2, rng=rng),
    )

    def test_none_source_rejected_everywhere(self):
        for call in self.strategies:
            with self.assertRaises(RandomSourceUnavailableError):
                call(None)

    def test_source_missing_methods_rejected(self):
        for call in self.strategies:
            with self.assertRaises(RandomSourceUnavailableError):
                call(object())

    def test_failing_source_wrapped_uniformly(self):
        class DeadSource:
            def random(self):
                raise OSError("entropy pool exhausted")

            def randrange(self, n):
                raise OSError("entropy pool exhausted")

        for call in self.strategies:
            with self.assertRaises(RandomSourceUnavailableError):
                call(DeadSource())

    def test_out_of_range_source_rejected(self):
        class BadSource:
            def random(self):
                return 1.5

            def randrange(self, n):
                return n  # off the end

        for call in self.strategies:
            with self.assertRaises(RandomSourceUnavailableError):
                call(BadSource())


if __name__ == "__main__":
    unittest.main()
