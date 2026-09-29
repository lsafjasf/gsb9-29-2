"""无偏性回归测试：经验频率与理论概率对比（卡方拟合优度，固定种子）。

详细数据表由 scripts/unbiasedness_report.py 生成到 docs/UNBIASEDNESS.md。
"""

import unittest
from collections import Counter

from sampling import (
    Sampler, UniformSampling, WeightedSampling, StratifiedSampling,
    SeededRandom, reservoir_uniform, chi2_statistic, chi2_sf,
)

ALPHA = 1e-4  # 固定种子下确定性通过；阈值宽松以避免统计噪声误判


class TestUniformUnbiased(unittest.TestCase):
    def test_inclusion_frequency(self):
        n, k, trials = 8, 3, 40000
        sampler = Sampler(UniformSampling(), SeededRandom(20260930))
        counts = Counter()
        for _ in range(trials):
            counts.update(sampler.sample(list(range(n)), k))
        observed = [counts[i] for i in range(n)]
        expected = [trials * k / n] * n
        stat = chi2_statistic(observed, expected)
        self.assertGreater(chi2_sf(stat, n - 1), ALPHA)


class TestWeightedUnbiased(unittest.TestCase):
    def test_single_draw_frequency(self):
        weights = [5.0, 3.0, 2.0, 0.0]
        trials = 60000
        sampler = Sampler(WeightedSampling(weights), SeededRandom(20260930))
        counts = Counter()
        for _ in range(trials):
            counts.update(sampler.sample(list(range(4)), 1))
        total = sum(weights)
        observed = [counts[i] for i in range(4)]
        expected = [trials * w / total for w in weights]
        stat = chi2_statistic(observed, expected)
        self.assertGreater(chi2_sf(stat, 3), ALPHA)
        self.assertEqual(counts[3], 0)  # 零权重从不出现


class TestStratifiedUnbiased(unittest.TestCase):
    def test_stratum_inclusion_frequency(self):
        sizes = [55, 30, 15]
        k = 10  # 最大余数法分配为 [6, 3, 1]
        pop = [(s, i) for s, size in enumerate(sizes) for i in range(size)]
        trials = 30000
        sampler = Sampler(StratifiedSampling(lambda x: x[0]), SeededRandom(20260930))
        stratum_counts = Counter()
        for _ in range(trials):
            for rec in sampler.sample(pop, k):
                stratum_counts[rec[0]] += 1
        alloc = [6, 3, 1]
        observed = [stratum_counts[s] for s in range(3)]
        expected = [trials * a for a in alloc]
        stat = chi2_statistic(observed, expected)
        self.assertGreater(chi2_sf(stat, 2), ALPHA)


class TestStreamUnbiased(unittest.TestCase):
    def test_reservoir_inclusion_frequency(self):
        n, k, trials = 8, 3, 40000
        counts = Counter()
        for t in range(trials):
            got = reservoir_uniform(iter(range(n)), k, SeededRandom(t))
            counts.update(got)
        observed = [counts[i] for i in range(n)]
        expected = [trials * k / n] * n
        stat = chi2_statistic(observed, expected)
        self.assertGreater(chi2_sf(stat, n - 1), ALPHA)


if __name__ == "__main__":
    unittest.main()
