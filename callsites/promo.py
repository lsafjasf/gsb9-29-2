"""Refactored promo call site (was legacy.promo)."""

from sampling import WeightedSampling


def draw_by_weight(items, weights, k, *, rng):
    return WeightedSampling(weights, replace=True).sample(items, k, rng=rng)
