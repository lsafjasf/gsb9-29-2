"""Unified error types for the sampling component.

All strategies raise the same error types for the same parameter problems,
so callers only need one set of handlers regardless of strategy.
"""


class SamplingError(Exception):
    """Base class for every error raised by the sampling component."""


class SampleSizeError(SamplingError, ValueError):
    """Requested sample size exceeds the population (without replacement)."""

    def __init__(self, k, n):
        self.k = k
        self.n = n
        super().__init__(
            "sample size %d exceeds population size %d "
            "(sampling without replacement)" % (k, n)
        )


class WeightError(SamplingError, ValueError):
    """Invalid weight vector (negative entry, all-zero total, bad length)."""


class RandomSourceUnavailableError(SamplingError, RuntimeError):
    """The injected random source is missing, malformed, or failed mid-draw."""
