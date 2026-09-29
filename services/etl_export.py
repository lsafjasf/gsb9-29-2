"""ETL 导出模块 —— 重构后：按权重抽样委托给统一组件。

行为变化（相对 legacy.etl_export）：
- 随机源改为显式注入，不再内部新建 random.Random()；
- 负权重不再静默清零，抛 WeightError；全零权重抛 WeightError；
- k > n 时抛 SampleSizeError。
有效输入范围内与旧实现逐位等价（见 tests/test_differential.py）。
"""

from sampling import Sampler, WeightedSampling


def sample_records(records, weights, k, rng):
    return Sampler(WeightedSampling(weights), rng).sample(records, k)
