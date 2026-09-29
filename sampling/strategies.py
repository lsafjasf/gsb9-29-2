"""Unified sampling strategies.

All strategies share one call signature and one set of semantics:

    strategy.sample(population, k, *, rng) -> list

- ``population``: any finite iterable of items.
- ``k``: requested sample size (0 <= k; k <= len(population) unless the
  strategy samples with replacement).
- ``rng``: an injected RandomSource (see sampling.source); never created
  or seeded inside a strategy.

Errors are uniform across strategies (see sampling.errors):
SampleSizeError, WeightError, RandomSourceUnavailableError.
"""

from .errors import SampleSizeError, WeightError
from .source import ensure_source, next_float, next_index


def _check_k(k, n):
    if isinstance(k, bool) or not isinstance(k, int):
        raise TypeError("k must be an int, got %r" % (k,))
    if k < 0:
        raise ValueError("k must be >= 0, got %d" % k)
    if k > n:
        raise SampleSizeError(k, n)


class UniformSampling:
    """Equal-probability sampling without replacement.

    Algorithm: in-place Fisher-Yates shuffle of a copy, then take the
    first k items. This consumes randomness exactly like
    ``random.shuffle(x); x[:k]`` on a ``random.Random``-compatible source,
    which is what the legacy lottery call site did.
    """

    def sample(self, population, k, *, rng):
        items = list(population)
        _check_k(k, len(items))
        rng = ensure_source(rng)
        for i in range(len(items) - 1, 0, -1):
            j = next_index(rng, i + 1)
            items[i], items[j] = items[j], items[i]
        return items[:k]


class WeightedSampling:
    """Sampling proportional to weights.

    weights: non-negative numbers, at least one positive. Zero-weight items
    are never selected (strict-inequality scan; a draw of exactly 0.0 still
    lands on the first *positive*-weight item).

    replace=True:  k independent draws, k may exceed the population size.
    replace=False: Efraimidis-Spirakis (A-Res) exponential race; each item
                   gets key u**(1/w) and the k largest keys win. Zero-weight
                   items are excluded without consuming randomness.
    """

    def __init__(self, weights, *, replace=True):
        weights = [float(w) for w in weights]
        for w in weights:
            if w < 0:
                raise WeightError("weights must be >= 0, got %r" % (w,))
        total = sum(weights)
        if total <= 0:
            raise WeightError("total weight must be positive (all weights are zero)")
        self._weights = weights
        self._total = total
        self._replace = replace

    @property
    def replace(self):
        return self._replace

    def sample(self, population, k, *, rng):
        items = list(population)
        if isinstance(k, bool) or not isinstance(k, int):
            raise TypeError("k must be an int, got %r" % (k,))
        if k < 0:
            raise ValueError("k must be >= 0, got %d" % k)
        if len(items) != len(self._weights):
            raise WeightError(
                "got %d weights for a population of %d"
                % (len(self._weights), len(items))
            )
        rng = ensure_source(rng)
        if self._replace:
            return [items[self._draw_one(rng)] for _ in range(k)]
        return self._sample_without_replacement(items, k, rng)

    def _draw_one(self, rng):
        r = next_float(rng) * self._total
        acc = 0.0
        for i, w in enumerate(self._weights):
            acc += w
            if r < acc:
                return i
        # Float round-off guard: r is strictly below total in exact math.
        for i in range(len(self._weights) - 1, -1, -1):
            if self._weights[i] > 0:
                return i
        raise WeightError("total weight must be positive")  # unreachable

    def _sample_without_replacement(self, items, k, rng):
        keyed = []
        for item, w in zip(items, self._weights):
            if w == 0:
                continue
            u = next_float(rng)
            keyed.append((u ** (1.0 / w), item))
        if len(keyed) < k:
            raise SampleSizeError(k, len(keyed))
        keyed.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in keyed[:k]]


class StratifiedSampling:
    """Stratified sampling with proportional allocation.

    The population is grouped by ``key(item)``; each stratum receives a
    quota proportional to its size via the largest-remainder method, so
    quotas always sum to exactly k. Within each stratum, UniformSampling
    draws without replacement. Strata are processed in first-seen order
    and share the single injected rng, so results are reproducible.
    """

    def __init__(self, key):
        if not callable(key):
            raise TypeError("key must be callable")
        self._key = key

    def sample(self, population, k, *, rng):
        items = list(population)
        _check_k(k, len(items))
        rng = ensure_source(rng)
        groups = {}
        for item in items:
            groups.setdefault(self._key(item), []).append(item)
        quotas = self._allocate(k, [len(m) for m in groups.values()])
        uniform = UniformSampling()
        out = []
        for members, quota in zip(groups.values(), quotas):
            out.extend(uniform.sample(members, quota, rng=rng))
        return out

    @staticmethod
    def _allocate(k, sizes):
        total = sum(sizes)
        raw = [k * s / total for s in sizes]
        base = [int(x) for x in raw]  # floor (values are non-negative)
        remainder = k - sum(base)
        # Largest fractional parts first; ties keep stratum order stable.
        order = sorted(range(len(sizes)), key=lambda i: (-(raw[i] - base[i]), i))
        for i in order[:remainder]:
            base[i] += 1
        return base
