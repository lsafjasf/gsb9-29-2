"""BI 报表模块 —— 重构后：等概率抽样委托给统一组件。

行为变化（相对 legacy.bi_report）：
- 随机源改为显式注入（SeededRandom(seed) 即可复现），不再使用全局 random；
- k > n 时抛带明确信息的 SampleSizeError，不再是底层 ValueError。
有效输入范围内与旧实现逐位等价（见 tests/test_differential.py）。
"""

from sampling import Sampler, UniformSampling


def sample_rows(rows, k, rng):
    return Sampler(UniformSampling(), rng).sample(rows, k)
