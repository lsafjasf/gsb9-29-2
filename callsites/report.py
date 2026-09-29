"""Refactored reporting call site (was legacy.report)."""

from sampling import StratifiedSampling


def stratified_sample(rows, key, k, *, rng):
    return StratifiedSampling(key).sample(rows, k, rng=rng)
