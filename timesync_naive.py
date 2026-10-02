"""Buggy baseline synchronizer, kept only to reproduce the reported defects.

Defects (intentional, do not use in production):
  * cumulative mean over ALL samples since boot -> old samples dominate,
    clock skew (drift) is never estimated, so the residual grows without
    bound on long runs;
  * no outlier filtering -> delay spikes pollute the estimate;
  * no step detection -> a clock step is silently averaged in ("eaten")
    instead of triggering re-convergence.
"""


class NaiveSynchronizer:
    def __init__(self):
        self._sum = 0.0
        self._n = 0

    def update(self, t, offset, delay):
        self._sum += offset
        self._n += 1

    def note_poll_failure(self, t):
        pass

    def offset_estimate(self, t=None):
        if self._n == 0:
            return None
        return self._sum / self._n

    def status(self, t):
        return "synced" if self._n else "acquiring"

    def stats(self):
        return {"total": self._n, "filtered": 0, "filter_ratio": 0.0}
