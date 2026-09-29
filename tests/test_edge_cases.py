"""边界情形：零权重、样本量大于总体、流式输入、随机源不可用。"""

import unittest

from sampling import (
    Sampler, UniformSampling, WeightedSampling, StratifiedSampling,
    SeededRandom, FuncSource,
    SampleSizeError, WeightError, RandomSourceUnavailable,
    reservoir_uniform, reservoir_weighted, reservoir_stratified,
)


class TestZeroWeights(unittest.TestCase):
    def test_zero_weight_never_selected(self):
        items = list(range(6))
        weights = [1, 0, 2, 0, 3, 0]
        sampler = Sampler(WeightedSampling(weights), SeededRandom(123))
        for _ in range(200):
            got = sampler.sample(items, 3)
            self.assertTrue(set(got) <= {0, 2, 4})

    def test_all_zero_weights_rejected(self):
        with self.assertRaises(WeightError):
            Sampler(WeightedSampling([0, 0, 0]), SeededRandom(1)).sample([1, 2, 3], 1)

    def test_negative_and_nan_weights_rejected(self):
        for bad in ([1, -2, 3], [1, float("nan"), 3], [1, float("inf"), 3]):
            with self.assertRaises(WeightError, msg=str(bad)):
                Sampler(WeightedSampling(bad), SeededRandom(1)).sample([1, 2, 3], 1)

    def test_weight_length_mismatch_rejected(self):
        with self.assertRaises(WeightError):
            Sampler(WeightedSampling([1, 2]), SeededRandom(1)).sample([1, 2, 3], 1)

    def test_stream_zero_weight_only_when_underfilled(self):
        pairs = iter([("a", 1.0), ("b", 0.0), ("c", 2.0)])
        got = reservoir_weighted(pairs, 2, SeededRandom(5))
        self.assertNotIn("b", got)
        pairs = iter([("a", 1.0), ("b", 0.0)])
        got = reservoir_weighted(pairs, 2, SeededRandom(5))
        self.assertIn("b", got)  # 正权重不足 k 个时才可能进入


class TestSampleSizeExceedsPopulation(unittest.TestCase):
    def test_all_strategies_raise_consistently(self):
        pop = list(range(5))
        cases = [
            Sampler(UniformSampling(), SeededRandom(1)),
            Sampler(WeightedSampling([1] * 5), SeededRandom(1)),
            Sampler(StratifiedSampling(lambda x: x % 2), SeededRandom(1)),
        ]
        for sampler in cases:
            with self.assertRaises(SampleSizeError):
                sampler.sample(pop, 6)

    def test_negative_and_nonint_k_rejected(self):
        sampler = Sampler(UniformSampling(), SeededRandom(1))
        for bad in (-1, 2.5, "3"):
            with self.assertRaises(SampleSizeError):
                sampler.sample([1, 2, 3], bad)

    def test_k_equals_n_returns_all(self):
        pop = list(range(10))
        got = Sampler(UniformSampling(), SeededRandom(9)).sample(pop, 10)
        self.assertEqual(sorted(got), pop)

    def test_k_zero_returns_empty(self):
        pop = list(range(10))
        self.assertEqual(Sampler(UniformSampling(), SeededRandom(9)).sample(pop, 0), [])


class TestStreamingInput(unittest.TestCase):
    def test_uniform_stream_from_generator(self):
        gen = (x for x in range(100))
        got = reservoir_uniform(gen, 10, SeededRandom(3))
        self.assertEqual(len(got), 10)
        self.assertTrue(all(0 <= x < 100 for x in got))

    def test_uniform_stream_clamps_when_k_exceeds_n(self):
        got = reservoir_uniform(iter([1, 2, 3]), 10, SeededRandom(3))
        self.assertEqual(sorted(got), [1, 2, 3])

    def test_weighted_stream_from_generator(self):
        pairs = ((i, float(i % 5)) for i in range(50))
        got = reservoir_weighted(pairs, 8, SeededRandom(4))
        self.assertEqual(len(got), 8)

    def test_stratified_stream_from_generator(self):
        gen = (("k%d" % (i % 3), i) for i in range(90))
        got = reservoir_stratified(gen, lambda kv: kv[0], 9, SeededRandom(6))
        self.assertEqual(len(got), 9)

    def test_stratified_stream_clamps_when_k_exceeds_n(self):
        gen = (("a", i) for i in range(4))
        got = reservoir_stratified(gen, lambda kv: kv[0], 10, SeededRandom(6))
        self.assertEqual(len(got), 4)

    def test_stream_bad_k_rejected(self):
        with self.assertRaises(SampleSizeError):
            reservoir_uniform(iter([1, 2]), -1, SeededRandom(1))


class TestRandomSourceUnavailable(unittest.TestCase):
    def test_none_source_rejected(self):
        with self.assertRaises(RandomSourceUnavailable):
            Sampler(UniformSampling(), None)

    def test_incomplete_source_rejected(self):
        class OnlyRandom:
            def random(self):
                return 0.5
        with self.assertRaises(RandomSourceUnavailable):
            Sampler(UniformSampling(), OnlyRandom())

    def test_broken_source_propagates(self):
        class Broken:
            def random(self):
                raise RandomSourceUnavailable("熵源故障")
            def randbelow(self, n):
                raise RandomSourceUnavailable("熵源故障")
        with self.assertRaises(RandomSourceUnavailable):
            Sampler(UniformSampling(), Broken()).sample([1, 2, 3], 1)

    def test_out_of_range_func_source_rejected(self):
        with self.assertRaises(RandomSourceUnavailable):
            Sampler(UniformSampling(), FuncSource(lambda: 1.5)).sample([1, 2, 3], 1)
        with self.assertRaises(RandomSourceUnavailable):
            FuncSource(lambda: -0.1).random()


if __name__ == "__main__":
    unittest.main()
