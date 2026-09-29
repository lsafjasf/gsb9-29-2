"""差分测试：每个原调用点在同一随机源下，新旧实现结果必须逐位一致。"""

import random
import unittest

from sampling import SeededRandom, FuncSource
import legacy.bi_report
import legacy.etl_export
import legacy.training_data
import services.bi_report
import services.etl_export
import services.training_data

SEEDS = [0, 1, 7, 42, 2024, 987654321]


def make_rows(n):
    return ["row-%03d" % i for i in range(n)]


def make_records(n):
    return [("rec-%03d" % i, "A" if i % 3 == 0 else ("B" if i % 3 == 1 else "C"))
            for i in range(n)]


class TestDifferentialBIReport(unittest.TestCase):
    """调用点 1：legacy.bi_report.sample_rows -> services.bi_report.sample_rows"""

    def test_same_seed_same_result(self):
        for n, k in [(10, 3), (50, 1), (50, 50), (100, 37), (7, 0)]:
            rows = make_rows(n)
            for seed in SEEDS:
                random.seed(seed)  # 旧实现用全局 random
                old = legacy.bi_report.sample_rows(rows, k)
                new = services.bi_report.sample_rows(rows, k, SeededRandom(seed))
                self.assertEqual(old, new, "n=%d k=%d seed=%d" % (n, k, seed))


class TestDifferentialETLExport(unittest.TestCase):
    """调用点 2：legacy.etl_export.sample_records -> services.etl_export.sample_records"""

    def test_same_seed_same_result(self):
        records = make_rows(60)
        # (weights, 允许的最大 k)：含零权重时 k 不能超过正权重条目数，
        # 否则旧实现本身会以 IndexError 崩溃（属非法输入区，见 docs/CALLSITES.md）
        weight_sets = [
            ([1.0] * 60, 60),
            ([i + 1 for i in range(60)], 60),          # 整数权重
            ([0.05 * (i + 1) for i in range(60)], 60),  # 比例权重（相对语义）
            ([10, 0, 3, 0, 1] * 12, 36),                # 含零权重（36 个正权重）
        ]
        for weights, max_k in weight_sets:
            for k in [1, 5, 30, max_k]:
                for seed in SEEDS:
                    old = legacy.etl_export.sample_records(records, weights, k, seed=seed)
                    new = services.etl_export.sample_records(
                        records, weights, k, SeededRandom(seed))
                    self.assertEqual(old, new, "k=%d seed=%d" % (k, seed))


class TestDifferentialTrainingData(unittest.TestCase):
    """调用点 3：legacy.training_data.sample_stratified -> services.training_data"""

    def test_same_source_same_result(self):
        records = make_records(97)
        key_fn = lambda rec: rec[1]
        for frac in [0.0, 0.1, 0.5, 0.77, 1.0]:
            for seed in SEEDS:
                old = legacy.training_data.sample_stratified(
                    records, key_fn, frac, random.Random(seed).random)
                new = services.training_data.sample_stratified(
                    records, key_fn, frac, FuncSource(random.Random(seed).random))
                self.assertEqual(old, new, "frac=%s seed=%d" % (frac, seed))


class TestCrossModuleConsistency(unittest.TestCase):
    """收敛目标：同一随机源下，不同模块对同一总体抽样结果一致（不再互相矛盾）。"""

    def test_uniform_consistent_across_modules(self):
        rows = make_rows(40)
        for seed in SEEDS:
            via_bi = services.bi_report.sample_rows(rows, 8, SeededRandom(seed))
            from sampling import Sampler, UniformSampling
            direct = Sampler(UniformSampling(), SeededRandom(seed)).sample(rows, 8)
            self.assertEqual(via_bi, direct)

    def test_uniform_weights_match_uniform_sampling(self):
        """等权重加权抽样与等概率抽样在分布上一致（此处验证零权重永不出现）。"""
        records = make_rows(10)
        weights = [1.0] * 9 + [0.0]
        for seed in SEEDS:
            got = services.etl_export.sample_records(
                records, weights, 9, SeededRandom(seed))
            self.assertNotIn("row-009", got)
            self.assertEqual(len(set(got)), 9)


if __name__ == "__main__":
    unittest.main()
