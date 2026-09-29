"""统一抽样组件：策略接口 + 注入式随机源 + 流式抽样 + 统计校验工具。"""

from .errors import (
    SamplingError,
    SampleSizeError,
    WeightError,
    RandomSourceUnavailable,
)
from .sources import SeededRandom, SystemRandomSource, FuncSource, require_source
from .strategies import (
    SamplingStrategy,
    UniformSampling,
    WeightedSampling,
    StratifiedSampling,
    largest_remainder,
)
from .sampler import Sampler
from .stream import reservoir_uniform, reservoir_weighted, reservoir_stratified
from .stats import chi2_statistic, chi2_sf

__all__ = [
    "SamplingError", "SampleSizeError", "WeightError", "RandomSourceUnavailable",
    "SeededRandom", "SystemRandomSource", "FuncSource", "require_source",
    "SamplingStrategy", "UniformSampling", "WeightedSampling", "StratifiedSampling",
    "largest_remainder",
    "Sampler",
    "reservoir_uniform", "reservoir_weighted", "reservoir_stratified",
    "chi2_statistic", "chi2_sf",
]
