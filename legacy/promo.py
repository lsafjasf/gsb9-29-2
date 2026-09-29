"""Legacy promo draw (module B).

Creates a fresh unseeded Random() per call, so runs are not reproducible.
Weights are normalized to sum to 1 before scanning; the '<=' comparison
means a zero-weight item can be picked when random() returns exactly 0.0,
and all-zero weights crash with ZeroDivisionError.
"""

import random


def draw_by_weight(items, weights, k):
    """Draw k items with replacement, proportional to weights."""
    rng = random.Random()
    total = float(sum(weights))
    cumulative = []
    acc = 0.0
    for w in weights:
        acc += w / total
        cumulative.append(acc)
    picked = []
    for _ in range(k):
        r = rng.random()
        for idx, boundary in enumerate(cumulative):
            if r <= boundary:
                picked.append(items[idx])
                break
    return picked
