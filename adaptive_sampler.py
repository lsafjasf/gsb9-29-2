"""Adaptive sampler: keeps reported rate at a target while prioritizing important events.

Stdlib only, Python 3.7+.

Strategy
--------
- Important events (errors, slow requests, configured dimension matches) are
  ALWAYS kept, even if that alone exceeds the target rate.
- Normal events are kept with probability ``p`` via a credit (deficit-counter)
  sampler: each normal event adds ``p`` to a credit balance, and an event is
  kept whenever the balance reaches 1. This keeps the per-window reported
  count within 1 event of ``p * n`` (vs. ~sqrt(n) noise for Bernoulli
  sampling) while remaining unbiased for i.i.d. streams.
- ``p`` is adjusted once per window by a smoothed proportional controller:

      budget      = max(target_rate - important_incoming_rate, 0)
      desired_p   = clamp(budget / normal_incoming_rate, 0, 1)
      p          += smoothing * (desired_p - p)

  The controller reacts to a 10x traffic surge within 1-2 windows and raises
  ``p`` back towards 1.0 when traffic drops.
"""

import random
import time

__all__ = ["AdaptiveSampler", "WindowStats"]


class WindowStats:
    """Counters for the most recently completed control window."""

    __slots__ = (
        "elapsed", "incoming", "incoming_important", "incoming_normal",
        "kept", "kept_important", "kept_normal", "reported_rate", "probability",
    )

    def __init__(self, elapsed, incoming, incoming_important, incoming_normal,
                 kept, kept_important, kept_normal, probability):
        self.elapsed = elapsed
        self.incoming = incoming
        self.incoming_important = incoming_important
        self.incoming_normal = incoming_normal
        self.kept = kept
        self.kept_important = kept_important
        self.kept_normal = kept_normal
        self.reported_rate = kept / elapsed if elapsed > 0 else 0.0
        self.probability = probability

    def as_dict(self):
        return {name: getattr(self, name) for name in self.__slots__}


class AdaptiveSampler:
    def __init__(self, target_rate, window_sec=1.0, slow_threshold_ms=500.0,
                 important_dims=None, min_probability=0.0, smoothing=0.7,
                 rng=None, time_fn=None):
        if target_rate <= 0:
            raise ValueError("target_rate must be > 0")
        if window_sec <= 0:
            raise ValueError("window_sec must be > 0")
        if not 0.0 < smoothing <= 1.0:
            raise ValueError("smoothing must be in (0, 1]")
        if not 0.0 <= min_probability <= 1.0:
            raise ValueError("min_probability must be in [0, 1]")
        self.target_rate = float(target_rate)
        self.window_sec = float(window_sec)
        self.slow_threshold_ms = float(slow_threshold_ms)
        # important_dims: {"dim_name": {"value1", "value2"}, ...}
        self.important_dims = {
            key: set(values) for key, values in (important_dims or {}).items()
        }
        self.min_probability = float(min_probability)
        self.smoothing = float(smoothing)
        self._rng = rng if rng is not None else random.Random()
        self._time_fn = time_fn if time_fn is not None else time.monotonic

        self._p = 1.0
        self._credit = self._rng.random()  # random phase start
        self._window_start = self._time_fn()
        self._reset_window_counters()

        # Cumulative (all-time) counters.
        self.total_incoming = 0
        self.total_kept = 0
        self.total_important_incoming = 0
        self.total_important_kept = 0
        self.last_window = None  # type: WindowStats | None

    # ------------------------------------------------------------------ API

    def is_important(self, event):
        """An event is important if it is an error, slow, or matches important_dims."""
        if event.get("error"):
            return True
        if event.get("duration_ms", 0.0) >= self.slow_threshold_ms:
            return True
        dims = event.get("dims") or {}
        for key, values in self.important_dims.items():
            if dims.get(key) in values:
                return True
        return False

    def should_sample(self, event):
        """Return True if the event should be reported."""
        now = self._time_fn()
        self._maybe_adjust(now)

        self._win_incoming += 1
        self.total_incoming += 1

        if self.is_important(event):
            self._win_incoming_important += 1
            self._win_kept += 1
            self._win_kept_important += 1
            self.total_important_incoming += 1
            self.total_important_kept += 1
            self.total_kept += 1
            return True

        self._win_incoming_normal += 1
        self._credit += self._p
        if self._credit >= 1.0:
            self._credit -= 1.0
            self._win_kept += 1
            self._win_kept_normal += 1
            self.total_kept += 1
            return True
        return False

    @property
    def probability(self):
        """Current sampling probability applied to normal events."""
        return self._p

    def snapshot(self):
        """Current sampler state, useful for metrics export."""
        return {
            "probability": self._p,
            "target_rate": self.target_rate,
            "total_incoming": self.total_incoming,
            "total_kept": self.total_kept,
            "total_important_incoming": self.total_important_incoming,
            "total_important_kept": self.total_important_kept,
            "last_window": self.last_window.as_dict() if self.last_window else None,
        }

    # -------------------------------------------------------------- internal

    def _reset_window_counters(self):
        self._win_incoming = 0
        self._win_incoming_important = 0
        self._win_incoming_normal = 0
        self._win_kept = 0
        self._win_kept_important = 0
        self._win_kept_normal = 0

    def _maybe_adjust(self, now):
        elapsed = now - self._window_start
        if elapsed < self.window_sec:
            return

        important_rate = self._win_incoming_important / elapsed
        normal_rate = self._win_incoming_normal / elapsed

        budget = max(self.target_rate - important_rate, 0.0)
        if normal_rate > 0:
            desired = min(1.0, budget / normal_rate)
        else:
            # No normal traffic this window: no information, keep p as-is.
            desired = self._p

        self._p = min(1.0, max(self.min_probability,
                               self._p + self.smoothing * (desired - self._p)))

        self.last_window = WindowStats(
            elapsed=elapsed,
            incoming=self._win_incoming,
            incoming_important=self._win_incoming_important,
            incoming_normal=self._win_incoming_normal,
            kept=self._win_kept,
            kept_important=self._win_kept_important,
            kept_normal=self._win_kept_normal,
            probability=self._p,
        )
        self._reset_window_counters()
        self._window_start = now
