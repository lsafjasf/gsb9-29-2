"""Helpers for unbiasedness verification (used by tests and reports)."""

import math


def inclusion_frequencies(draws, items):
    """Share of draws that contain each item (without-replacement metric)."""
    draws = list(draws)
    if not draws:
        raise ValueError("no draws")
    counts = {item: 0 for item in items}
    for draw in draws:
        for item in set(draw):
            counts[item] += 1
    return {item: counts[item] / len(draws) for item in items}


def position_frequencies(draws, items):
    """Share of all sampled positions occupied by each item.

    The right metric for with-replacement draws: an item drawn with
    probability p per position occupies a fraction p of all positions.
    """
    counts = {item: 0 for item in items}
    total = 0
    for draw in draws:
        for item in draw:
            counts[item] += 1
            total += 1
    if total == 0:
        raise ValueError("no observations")
    return {item: counts[item] / total for item in items}


def binomial_tolerance(p, trials, sigma=6.0):
    """6-sigma (default) standard-error bound for a frequency estimate."""
    return sigma * math.sqrt(p * (1.0 - p) / trials)
