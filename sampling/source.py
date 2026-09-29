"""Random-source injection for the sampling component.

Every strategy receives its randomness through a single injected object
satisfying the RandomSource protocol:

    random()   -> float in [0.0, 1.0)
    randrange(n) -> int in [0, n)

This replaces the legacy situation where each module created/seeded its own
generator internally, which made results across modules irreconcilable.
"""

import random

from .errors import RandomSourceUnavailableError


class SeededSource:
    """Deterministic source backed by ``random.Random(seed)``.

    Use in tests, differential runs, and anywhere reproducibility matters.
    """

    def __init__(self, seed):
        self._rng = random.Random(seed)

    def random(self):
        return self._rng.random()

    def randrange(self, n):
        return self._rng.randrange(n)


class SystemSource:
    """Non-deterministic source backed by OS entropy (random.SystemRandom)."""

    def __init__(self):
        self._rng = random.SystemRandom()

    def random(self):
        return self._rng.random()

    def randrange(self, n):
        return self._rng.randrange(n)


class ModuleSource:
    """Wraps the global ``random`` module.

    Kept for parity with legacy call sites that drew from the global module;
    seed it with ``random.seed(...)`` exactly as the legacy code expected.
    """

    def random(self):
        return random.random()

    def randrange(self, n):
        return random.randrange(n)


def ensure_source(rng):
    """Validate an injected random source without consuming randomness."""
    if rng is None:
        raise RandomSourceUnavailableError(
            "no random source provided; inject one explicitly "
            "(e.g. sampling.SeededSource(seed) or sampling.SystemSource())"
        )
    for name in ("random", "randrange"):
        if not callable(getattr(rng, name, None)):
            raise RandomSourceUnavailableError(
                "random source %r does not provide a callable %s()" % (rng, name)
            )
    return rng


def next_float(rng):
    """Draw one float in [0.0, 1.0), wrapping source failures uniformly."""
    try:
        value = rng.random()
    except RandomSourceUnavailableError:
        raise
    except Exception as exc:
        raise RandomSourceUnavailableError(
            "random source %r failed in random(): %s" % (rng, exc)
        ) from exc
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RandomSourceUnavailableError(
            "random source %r returned non-numeric value %r" % (rng, value)
        )
    if not 0.0 <= value < 1.0:
        raise RandomSourceUnavailableError(
            "random source %r returned out-of-range value %r" % (rng, value)
        )
    return float(value)


def next_index(rng, n):
    """Draw one index in [0, n), wrapping source failures uniformly."""
    try:
        value = rng.randrange(n)
    except RandomSourceUnavailableError:
        raise
    except Exception as exc:
        raise RandomSourceUnavailableError(
            "random source %r failed in randrange(%d): %s" % (rng, n, exc)
        ) from exc
    if isinstance(value, bool) or not isinstance(value, int):
        raise RandomSourceUnavailableError(
            "random source %r returned non-integer index %r" % (rng, value)
        )
    if not 0 <= value < n:
        raise RandomSourceUnavailableError(
            "random source %r returned out-of-range index %r for n=%d"
            % (rng, value, n)
        )
    return value
