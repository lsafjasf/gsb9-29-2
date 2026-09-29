"""Legacy stratified sampler for reporting (module C).

Uses a hard-coded seed, so every run returns the same "random" sample.
Proportional quotas are truncated with int(), so the remainders are
dropped and the result usually contains fewer than k rows.
"""

import random

_SEED = 2024


def stratified_sample(rows, key, k):
    """Sample k rows, proportionally allocated across strata."""
    rng = random.Random(_SEED)
    groups = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
    total = len(rows)
    out = []
    for members in groups.values():
        quota = int(k * len(members) / total)
        pool = list(members)
        for i in range(len(pool) - 1, 0, -1):
            j = rng.randrange(i + 1)
            pool[i], pool[j] = pool[j], pool[i]
        out.extend(pool[:quota])
    return out
