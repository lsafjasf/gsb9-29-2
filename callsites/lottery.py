"""Refactored lottery call site (was legacy.lottery)."""

from sampling import UniformSampling

_uniform = UniformSampling()


def pick_winners(records, k, *, rng):
    return _uniform.sample(records, k, rng=rng)
