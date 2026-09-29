"""Buggy reusable barrier -- the "before" version kept only for reproduction.

Known production bugs (do NOT use):
  1. Lost wakeup: the last arrival calls ``notify()`` instead of
     ``notify_all()``, so only one waiter is woken; the rest sleep until
     their timeout (or forever without a timeout).
  2. State residue: ``_count`` is reset *before* waiters are released, and
     a timeout/exception path leaks the arrival count, so the next round
     starts with a stale counter and can release early with fewer than
     ``parties`` threads actually arrived.
  3. No generation counter: a waiter from round N can consume a wakeup
     meant for round N+1.
  4. No abort/broken state: one thread dying or timing out leaves the
     remaining threads blocked forever.
"""

import threading


class BuggyBarrier:
    def __init__(self, parties):
        if parties < 1:
            raise ValueError("parties must be >= 1")
        self._parties = parties
        self._count = 0
        self._cond = threading.Condition()

    @property
    def parties(self):
        return self._parties

    @property
    def n_waiting(self):
        with self._cond:
            return self._count

    def wait(self, timeout=None):
        with self._cond:
            self._count += 1
            if self._count == self._parties:
                # BUG: state reset happens before anyone is released, and
                # only ONE waiter is notified.
                self._count = 0
                self._cond.notify()
                return 0
            # BUG: no generation check -- a missed/stale wakeup sleeps on.
            # BUG: on timeout the count is left behind, polluting the
            # next round.
            if not self._cond.wait(timeout):
                raise TimeoutError("barrier wait timed out")
            return 1
