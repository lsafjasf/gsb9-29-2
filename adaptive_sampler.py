"""Adaptive traffic sampler (Python 3, standard library only).

Dynamically adjusts the sampling ratio so the reported event rate tracks a
configurable target, while important events (errors, slow requests, specific
dimension values) are always kept.

Design
------
- A proportional controller re-tunes the sampling probability ``p`` once per
  window::

      p = clamp((target_rate - important_rate) / normal_input_rate)

  so the *normal* traffic only fills the budget left over by important events.
- Normal events are selected with per-dimension-key deficit counters: each
  arrival of key ``k`` adds ``p`` to ``deficit[k]`` and an event is emitted
  whenever the deficit reaches 1.  Over time the per-key sampled count stays
  within +-1 of ``p * key_count``, which keeps the sampled distribution
  faithful to the input distribution (verifiable, near-deterministic).
- A per-window emission cap (``budget * window * slack``) bounds the
  overshoot during sudden traffic bursts before the controller reacts.
- Important events bypass sampling entirely and are always reported.

No threads, no dependencies; the clock is injectable for deterministic tests.
"""

import time

__all__ = ["AdaptiveSampler", "default_importance"]


def default_importance(event, slow_ms=500.0, dimension_values=None):
    """Default importance rule: errors, slow requests, or matched dimensions.

    ``dimension_values`` maps a dimension name to a container of values that
    mark the event as important, e.g. ``{"tier": {"gold"}}``.
    """
    if event.get("error"):
        return True
    if event.get("latency_ms", 0.0) >= slow_ms:
        return True
    dims = event.get("dimensions") or {}
    for name, values in (dimension_values or {}).items():
        if dims.get(name) in values:
            return True
    return False


class AdaptiveSampler:
    """Rate-targeted adaptive sampler.

    Parameters
    ----------
    target_rate:    desired reported events per second (important + normal).
    window:         controller adjustment interval, seconds.
    key_dimensions: dimension names whose joint values define the strata
                    preserved by the deficit sampler.
    importance_fn:  ``event -> bool``; important events are always reported.
    clock:          monotonic clock, injectable for tests.
    p_min:          lower bound for the sampling probability.
    slack:          per-window emission cap = budget * window * slack; bounds
                    burst overshoot before the controller reacts.
    """

    def __init__(self, target_rate, window=1.0, key_dimensions=(),
                 importance_fn=None, clock=time.monotonic,
                 p_min=1e-6, slack=1.2):
        if target_rate <= 0:
            raise ValueError("target_rate must be positive")
        if window <= 0:
            raise ValueError("window must be positive")
        self.target_rate = float(target_rate)
        self.window = float(window)
        self.key_dimensions = tuple(key_dimensions)
        self._important_fn = importance_fn or default_importance
        self._clock = clock
        self.p_min = float(p_min)
        self.slack = float(slack)

        self.p = 1.0  # current sampling probability for normal events
        self._deficit = {}

        # per-window counters
        self._win_start = self._clock()
        self._win_normal_in = 0
        self._win_normal_out = 0
        self._win_important = 0
        self._win_keys = set()
        self._win_normal_cap = self.target_rate * self.window * self.slack

        # cumulative stats
        self.total_in = 0
        self.total_out = 0
        self.important_out = 0
        self.key_in = {}
        self.key_out = {}
        # one entry per completed window:
        # {end, elapsed, in_rate, reported_rate, important_rate, p}
        self.history = []

    # ------------------------------------------------------------------ API

    def observe(self, event):
        """Feed one event; return True if it should be reported."""
        now = self._clock()
        if now - self._win_start >= self.window:
            self._roll_window(now)

        self.total_in += 1
        key = self._key(event)
        self.key_in[key] = self.key_in.get(key, 0) + 1
        self._win_keys.add(key)

        if self._important_fn(event):
            self._win_important += 1
            self.important_out += 1
            self._emit(key)
            return True

        self._win_normal_in += 1
        if self._win_normal_out >= self._win_normal_cap:
            return False
        deficit = self._deficit.get(key, 0.0) + self.p
        if deficit >= 1.0:
            self._deficit[key] = deficit - 1.0
            self._win_normal_out += 1
            self._emit(key)
            return True
        self._deficit[key] = deficit
        return False

    def stats(self):
        """Snapshot of cumulative counters."""
        return {
            "p": self.p,
            "total_in": self.total_in,
            "total_out": self.total_out,
            "important_out": self.important_out,
            "key_in": dict(self.key_in),
            "key_out": dict(self.key_out),
        }

    # ------------------------------------------------------------- internal

    def _key(self, event):
        dims = event.get("dimensions") or {}
        if not self.key_dimensions:
            return ("__all__",)
        return tuple(dims.get(name) for name in self.key_dimensions)

    def _emit(self, key):
        self.total_out += 1
        self.key_out[key] = self.key_out.get(key, 0) + 1

    def _roll_window(self, now):
        elapsed = now - self._win_start
        important_rate = self._win_important / elapsed
        normal_in_rate = self._win_normal_in / elapsed
        reported_rate = (self._win_important + self._win_normal_out) / elapsed

        budget = max(self.target_rate - important_rate, 0.0)
        if normal_in_rate > 0.0:
            desired_p = min(1.0, budget / normal_in_rate)
        else:
            desired_p = 1.0
        self.p = min(1.0, max(self.p_min, desired_p))

        self.history.append({
            "end": now,
            "elapsed": elapsed,
            "in_rate": (self._win_normal_in + self._win_important) / elapsed,
            "reported_rate": reported_rate,
            "important_rate": important_rate,
            "p": self.p,
        })

        # budget and emission cap for the next window
        self._win_normal_cap = budget * self.window * self.slack
        self._win_start = now
        self._win_normal_in = 0
        self._win_normal_out = 0
        self._win_important = 0
        # drop deficit state for strata not seen this window
        seen = self._win_keys
        self._deficit = {k: v for k, v in self._deficit.items() if k in seen}
        self._win_keys = set()
