"""Streaming (one-pass) sampling for inputs of unknown length.

Same contract as the in-memory strategies: rng is injected, k semantics
and error types are identical (SampleSizeError when the stream yields
fewer than k items).
"""

import heapq

from .errors import SampleSizeError, WeightError
from .source import ensure_source, next_float, next_index


def reservoir_sample(stream, k, *, rng):
    """Uniform k-sample from a one-pass iterable (Algorithm R)."""
    if isinstance(k, bool) or not isinstance(k, int):
        raise TypeError("k must be an int, got %r" % (k,))
    if k < 0:
        raise ValueError("k must be >= 0, got %d" % k)
    rng = ensure_source(rng)
    if k == 0:
        return []
    reservoir = []
    for i, item in enumerate(stream):
        if i < k:
            reservoir.append(item)
        else:
            j = next_index(rng, i + 1)
            if j < k:
                reservoir[j] = item
    if len(reservoir) < k:
        raise SampleSizeError(k, len(reservoir))
    return reservoir


def weighted_reservoir_sample(stream, k, *, rng):
    """Weighted k-sample from a one-pass iterable of (item, weight) pairs.

    A-Res (Efraimidis-Spirakis): each item gets key u**(1/w), the k largest
    keys are kept in a min-heap. Zero-weight items are skipped (never
    selected); negative weights raise WeightError.
    """
    if isinstance(k, bool) or not isinstance(k, int):
        raise TypeError("k must be an int, got %r" % (k,))
    if k < 0:
        raise ValueError("k must be >= 0, got %d" % k)
    rng = ensure_source(rng)
    if k == 0:
        return []
    heap = []
    for item, weight in stream:
        w = float(weight)
        if w < 0:
            raise WeightError("weights must be >= 0, got %r" % (w,))
        if w == 0:
            continue
        key = next_float(rng) ** (1.0 / w)
        if len(heap) < k:
            heapq.heappush(heap, (key, item))
        elif key > heap[0][0]:
            heapq.heapreplace(heap, (key, item))
    if len(heap) < k:
        raise SampleSizeError(k, len(heap))
    return [item for _, item in sorted(heap, key=lambda pair: -pair[0])]
