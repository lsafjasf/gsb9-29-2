"""Reusable (cyclic) barrier for threads -- fixed version.

Guarantees:
  * A round releases only after ALL ``parties`` threads have arrived.
  * Release uses a generation counter + ``notify_all``: no lost wakeups,
    and a waiter from round N can never consume round N+1's wakeup.
  * State is fully reset after every release (count back to 0, generation
    advanced), so nothing leaks into the next round.
  * Any exceptional path (timeout, abort, a participant dying) breaks the
    barrier and releases every waiter with ``BrokenBarrierError`` instead
    of letting them block forever. ``reset()`` makes it reusable again.

Standard library only.
"""

import threading
import time

__all__ = ["Barrier", "BrokenBarrierError", "BarrierTimeoutError"]


class BrokenBarrierError(RuntimeError):
    """Raised when the barrier is broken (aborted) or was broken while waiting."""


class BarrierTimeoutError(Exception):
    """Raised by the thread whose own wait() timed out (others get BrokenBarrierError)."""


class Barrier:
    def __init__(self, parties):
        if parties < 1:
            raise ValueError("parties must be >= 1")
        self._parties = parties
        self._cond = threading.Condition()
        self._count = 0          # arrivals in the current generation
        self._generation = 0     # bumped on every release/abort/reset
        self._broken = False

    @property
    def parties(self):
        return self._parties

    @property
    def n_waiting(self):
        with self._cond:
            return self._count

    @property
    def broken(self):
        with self._cond:
            return self._broken

    @property
    def generation(self):
        with self._cond:
            return self._generation

    def wait(self, timeout=None):
        """Block until all parties arrive. Returns an arrival index
        (0 .. parties-1); exactly one thread per round gets 0."""
        with self._cond:
            if self._broken:
                raise BrokenBarrierError("barrier is broken")
            generation = self._generation
            self._count += 1
            index = self._parties - self._count
            try:
                if self._count == self._parties:
                    self._release_locked()
                    return 0
                if timeout is None:
                    while generation == self._generation and not self._broken:
                        self._cond.wait()
                else:
                    deadline = time.monotonic() + timeout
                    while generation == self._generation and not self._broken:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise BarrierTimeoutError(
                                "barrier wait timed out after %ss" % timeout
                            )
                        self._cond.wait(remaining)
                if self._broken:
                    raise BrokenBarrierError("barrier was aborted while waiting")
                return index
            except Exception:
                # Never let one failure strand the other waiters.
                self._abort_locked()
                raise

    def abort(self):
        """Break the barrier: all current and future waiters get
        BrokenBarrierError until reset() is called."""
        with self._cond:
            self._abort_locked()

    def reset(self):
        """Restore the barrier to a clean, unbroken, empty state."""
        with self._cond:
            self._broken = False
            self._count = 0
            self._generation += 1
            self._cond.notify_all()

    def _release_locked(self):
        # Full state reset happens atomically with the wakeup, while the
        # lock is held: no thread can observe a half-reset barrier, and
        # the generation bump makes stale waiters immune to this wakeup.
        self._count = 0
        self._generation += 1
        self._cond.notify_all()

    def _abort_locked(self):
        if not self._broken:
            self._broken = True
            self._count = 0
            self._generation += 1
            self._cond.notify_all()
