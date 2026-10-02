"""Robust NTP-style clock synchronization (stdlib only; time is injected).

The caller performs the network exchange and feeds the four timestamps (or
the reduced (offset, delay) pair) to :meth:`Synchronizer.update`.  No global
clock or network access happens inside this module, which makes the whole
implementation deterministic and testable.

Defects of the naive baseline are fixed here:

1. Drift (clock skew) is tracked with a least-squares fit
   ``offset(t) = a + b * t`` over a sliding window, so the estimate can be
   evaluated at any time and stays accurate during long runs / holdover.
2. Abnormal measurements are rejected before the fit (MAD gate + delay
   gate).  The filter ratio is exposed via :meth:`Synchronizer.stats`.
3. A clock step is distinguished from a transient jitter spike: a spike is
   isolated, a step persists for several consecutive exchanges.  A step
   clears the window, forces re-convergence and is recorded in
   :attr:`Synchronizer.step_events`.
4. Long link outages put the synchronizer into ``holdover``: the last model
   keeps predicting (using the estimated skew) instead of going stale.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field


@dataclass
class Sample:
    t: float        # local receive time of the exchange (s)
    offset: float   # measured offset server - local (s)
    delay: float    # measured round-trip delay (s)


@dataclass
class StepEvent:
    t: float          # local time the step was confirmed (s)
    magnitude: float  # signed jump of the (server - local) offset (s)


def measure(t_local_send, t_server_recv, t_server_send, t_local_recv):
    """Reduce an NTP 4-timestamp exchange to (offset, round-trip delay)."""
    offset = (
        (t_server_recv - t_local_send)
        + (t_server_send - t_local_recv)
    ) / 2.0
    delay = (t_local_recv - t_local_send) - (
        t_server_send - t_server_recv
    )
    return offset, delay


class Synchronizer:
    FILTER_CRITERION = (
        "reject a sample when either: "
        "(1) |offset - median(offset)| > mad_k * 1.4826 * MAD, or "
        "(2) delay > min_delay + max(delay_gate_factor * "
        "(median(delay) - min_delay), delay_gate_floor). "
        "Clock step (not jitter): >= step_consecutive samples whose residual "
        "from the skew model exceeds max(step_sigma_k * sigma, step_floor); "
        "the window is then cleared and convergence restarts."
    )

    def __init__(
        self,
        window=60,
        min_samples=8,
        mad_k=3.5,
        delay_gate_factor=3.0,
        delay_gate_floor=1e-3,
        step_sigma_k=6.0,
        step_floor=5e-3,
        step_consecutive=3,
        holdover_after=120.0,
    ):
        self.window = window
        self.min_samples = min_samples
        self.mad_k = mad_k
        self.delay_gate_factor = delay_gate_factor
        self.delay_gate_floor = delay_gate_floor
        self.step_sigma_k = step_sigma_k
        self.step_floor = step_floor
        self.step_consecutive = step_consecutive
        self.holdover_after = holdover_after

        self.samples = deque(maxlen=window)
        self.step_events = []

        self.total_samples = 0
        self.filtered_samples = 0
        self.failed_polls = 0

        self._a = 0.0
        self._b = 0.0
        self._t0 = 0.0
        self._sigma = float("inf")
        self._model_ready = False
        self._consecutive_bad = 0
        self._last_good_t = None

    def update(self, t, offset, delay):
        """Feed one successful exchange (t = local receive time)."""
        self.total_samples += 1

        # Step detection runs BEFORE the sample enters the window, against the
        # current skew model.  A single huge value is jitter (counter resets);
        # only a run of persistent deviations is a clock step.
        # A genuine clock step shifts the offset but does NOT inflate the
        # round-trip delay, so delay outliers (network spikes) never count.
        if self._model_ready and self._delay_okay(delay):
            residual = offset - self._predict(t)
            if abs(residual) > self._step_threshold():
                self._consecutive_bad += 1
                if self._consecutive_bad >= self.step_consecutive:
                    self.step_events.append(StepEvent(t, residual))
                    self._reset()
            else:
                self._consecutive_bad = 0

        sample = Sample(float(t), float(offset), float(delay))
        self.samples.append(sample)
        self._last_good_t = sample.t
        self._refit(sample)

    def note_poll_failure(self, t):
        """Record a failed/timed-out exchange (link unavailable)."""
        self.failed_polls += 1

    def offset_estimate(self, t=None):
        """Estimated server-minus-local offset at local time ``t``.

        During holdover the model (including skew) keeps predicting.
        Returns None before the first successful exchange.
        """
        if self._last_good_t is None:
            return None
        if t is None:
            t = self._last_good_t
        return self._predict(t)

    def skew(self):
        """Estimated relative clock skew (s/s, i.e. ~ppm * 1e-6)."""
        return self._b if self._model_ready else 0.0

    def status(self, t):
        if self._last_good_t is None or not self._model_ready:
            return "acquiring"
        if t - self._last_good_t > self.holdover_after:
            return "holdover"
        return "synced"

    def stats(self):
        ratio = (
            self.filtered_samples / self.total_samples
            if self.total_samples
            else 0.0
        )
        return {
            "total": self.total_samples,
            "filtered": self.filtered_samples,
            "filter_ratio": ratio,
            "failed_polls": self.failed_polls,
            "window_size": len(self.samples),
            "skew": self.skew(),
            "residual_sigma": self._sigma,
            "steps": len(self.step_events),
        }

    def _reset(self):
        self.samples.clear()
        self._model_ready = False
        self._consecutive_bad = 0
        self._sigma = float("inf")

    def _predict(self, t):
        return self._a + self._b * (t - self._t0)

    def _step_threshold(self):
        return max(self.step_sigma_k * self._sigma, self.step_floor)

    def _delay_okay(self, delay):
        if len(self.samples) < 2:
            return True
        delays = sorted(s.delay for s in self.samples)
        dmin = delays[0]
        dmed = statistics.median(delays)
        gate = dmin + max(
            self.delay_gate_factor * (dmed - dmin),
            self.delay_gate_floor,
        )
        return delay <= gate

    def _filter(self, samples):
        offsets = [s.offset for s in samples]
        med = statistics.median(offsets)
        abs_dev = [abs(o - med) for o in offsets]
        mad = max(1.4826 * statistics.median(abs_dev), 1e-12)

        delays = sorted(s.delay for s in samples)
        dmin = delays[0]
        dmed = statistics.median(delays)
        delay_gate = dmin + max(
            self.delay_gate_factor * (dmed - dmin),
            self.delay_gate_floor,
        )

        kept = []
        for s in samples:
            if abs(s.offset - med) > self.mad_k * mad:
                continue
            if s.delay > delay_gate:
                continue
            kept.append(s)
        return kept

    def _refit(self, newest):
        samples = list(self.samples)
        kept = self._filter(samples)
        kept_ids = {id(s) for s in kept}
        if id(newest) not in kept_ids:
            self.filtered_samples += 1

        if not kept:
            # Whole window rejected: fall back to the newest raw sample but do
            # not trust it for step detection.
            kept = [samples[-1]]

        if len(kept) >= 2:
            t_ref = kept[-1].t
            xs = [s.t - t_ref for s in kept]
            ys = [s.offset for s in kept]
            n = len(xs)
            sx = sum(xs)
            sy = sum(ys)
            sxx = sum(x * x for x in xs)
            sxy = sum(x * y for x, y in zip(xs, ys))
            denom = n * sxx - sx * sx
            if denom > 0.0:
                b = (n * sxy - sx * sy) / denom
            else:
                b = 0.0
            a = (sy - b * sx) / n
            residuals = [y - (a + b * x) for x, y in zip(xs, ys)]
            r_med = statistics.median(residuals)
            sigma = max(
                1.4826 * statistics.median([abs(r - r_med) for r in residuals]),
                1e-12,
            )
        else:
            t_ref = kept[0].t
            a, b, sigma = kept[0].offset, 0.0, float("inf")

        self._t0 = t_ref
        self._a = a
        self._b = b
        self._sigma = sigma
        self._model_ready = len(self.samples) >= self.min_samples
