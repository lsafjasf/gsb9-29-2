"""Unified sampling component.

One strategy interface, one injected random source, one set of semantics:

    from sampling import UniformSampling, WeightedSampling, StratifiedSampling
    from sampling import SeededSource, SystemSource

    UniformSampling().sample(population, k, rng=SeededSource(42))
    WeightedSampling(weights, replace=False).sample(population, k, rng=rng)
    StratifiedSampling(key=lambda r: r["region"]).sample(rows, k, rng=rng)
"""

from .errors import (
    RandomSourceUnavailableError,
    SampleSizeError,
    SamplingError,
    WeightError,
)
from .source import ModuleSource, SeededSource, SystemSource, ensure_source
from .strategies import StratifiedSampling, UniformSampling, WeightedSampling
from .streaming import reservoir_sample, weighted_reservoir_sample

__all__ = [
    "ModuleSource",
    "RandomSourceUnavailableError",
    "SampleSizeError",
    "SamplingError",
    "SeededSource",
    "StratifiedSampling",
    "SystemSource",
    "UniformSampling",
    "WeightError",
    "WeightedSampling",
    "ensure_source",
    "reservoir_sample",
    "weighted_reservoir_sample",
]
