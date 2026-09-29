"""Fixed reusable barrier (sense-reversing, generation-tagged).

Guarantees:
- All `parties` threads must arrive before anyone is released.
- After a release the state is fully reset (count=0, not broken,
  generation advanced), so rounds never leak into each other.
- Exception paths (timeout, abort, BaseException while waiting) break the
  barrier and release every waiter with BrokenBarrierError; nobody blocks
  forever. reset() makes a broken barrier usable again.

Standard library only.
"""

import threading
import time


class BrokenBarrierError(RuntimeError):
    pass


class Barrier:
    def __init__(self, parties):
        if parties < 1:
            raise ValueError("parties must be >= 1")
        self._parties = parties
        self._cond = threading.Condition()
        self._count = 0          # arrivals in the current generation
        self._generation = 0     # bumped on every release/reset
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
        with self._cond:
            if self._broken:
                raise BrokenBarrierError("barrier is broken")
            generation = self._generation
            self._count += 1
            try:
                if self._count == self._parties:
                    self._release_locked()
                    return
                if timeout is None:
                    while generation == self._generation and not self._broken:
                        self._cond.wait()
                else:
                    deadline = time.monotonic() + timeout
                    while generation == self._generation and not self._broken:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self._abort_locked()
                            raise BrokenBarrierError("barrier wait timed out")
                        self._cond.wait(remaining)
                if self._broken:
                    raise BrokenBarrierError("barrier broken while waiting")
            except BaseException:
                # Leaving without this generation being released: break the
                # barrier so no waiter is left blocked forever.
                if generation == self._generation and not self._broken:
                    self._abort_locked()
                raise

    def abort(self):
        """Break the barrier immediately and release all waiters."""
        with self._cond:
            self._abort_locked()

    def reset(self):
        """Restore a broken (or in-use) barrier to a clean state."""
        with self._cond:
            self._broken = False
            self._count = 0
            self._generation += 1
            self._cond.notify_all()

    def _release_locked(self):
        # Full state reset for the next round, generation tag prevents
        # mixing old sleepers with new arrivals (no lost wakeup).
        self._count = 0
        self._generation += 1
        self._cond.notify_all()

    def _abort_locked(self):
        self._broken = True
        self._count = 0
        self._cond.notify_all()
