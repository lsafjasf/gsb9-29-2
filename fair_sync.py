"""Fair synchronization primitives implemented with the standard library only.

FairSemaphore
    A bounded, strictly FIFO semaphore. Waiters are served in arrival
    order, acquire() supports timeouts, and release() validates the
    permit count (it can never exceed the construction-time maximum).

FairBarrier
    A generation-based cyclic barrier. A generation is released exactly
    once, only when all parties have arrived. wait() supports timeouts;
    a timeout breaks the current generation (all its waiters get
    BrokenBarrierError) and the barrier automatically resets so the next
    generation can proceed.
"""

from __future__ import annotations

import threading
import time
from collections import deque

__all__ = ["FairSemaphore", "FairBarrier", "BrokenBarrierError"]


class _Token:
    """One queued waiter. Granted permits are delivered through the token."""

    __slots__ = ("granted",)

    def __init__(self) -> None:
        self.granted = False


class FairSemaphore:
    """Bounded semaphore with strict FIFO wakeup order.

    Invariants (hold at every lock release point):
      * 0 <= self._value <= self._max
      * self._queue non-empty  ==>  self._value == 0
      * a token is in the queue  <=>  it has not been granted
    """

    def __init__(self, value: int = 1) -> None:
        if not isinstance(value, int) or value < 1:
            raise ValueError("semaphore initial value must be an integer >= 1")
        self._max = value
        self._value = value
        self._cond = threading.Condition()
        self._queue: deque[_Token] = deque()

    @property
    def max_value(self) -> int:
        return self._max

    @property
    def value(self) -> int:
        """Number of currently free permits (snapshot under the lock)."""
        with self._cond:
            return self._value

    @property
    def waiters(self) -> int:
        with self._cond:
            return len(self._queue)

    def acquire(self, timeout: float | None = None) -> bool:
        """Acquire one permit, FIFO-fair.

        Returns True on success. With a timeout, returns False if the
        permit was not granted within ``timeout`` seconds; a timed-out
        waiter never consumes a permit granted concurrently.
        """
        with self._cond:
            # Fast path: nobody is waiting and a permit is free.
            if not self._queue and self._value > 0:
                self._value -= 1
                return True

            token = _Token()
            self._queue.append(token)

            if timeout is None:
                while not token.granted:
                    self._cond.wait()
                return True

            deadline = time.monotonic() + max(timeout, 0.0)
            while not token.granted:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    # We hold the lock, so a non-granted token is still
                    # queued: remove it so it can never consume a permit.
                    self._queue.remove(token)
                    return False
                self._cond.wait(remaining)
            return True

    def release(self) -> None:
        """Release one permit.

        The permit is handed directly to the longest-waiting thread, if
        any. Raises ValueError if the permit count would exceed the
        construction-time maximum (over-release / count mismatch).
        """
        with self._cond:
            if self._queue:
                token = self._queue.popleft()
                token.granted = True
                # Permit transfers to the waiter; _value stays 0.
                self._cond.notify_all()
                return
            if self._value >= self._max:
                raise ValueError(
                    "release() would exceed the maximum permit count "
                    f"({self._max})"
                )
            self._value += 1
            self._cond.notify_all()

    def locked(self) -> bool:
        with self._cond:
            return self._value == 0

    def __enter__(self) -> "FairSemaphore":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def __repr__(self) -> str:
        with self._cond:
            return (
                f"FairSemaphore(value={self._value}/{self._max}, "
                f"waiters={len(self._queue)})"
            )


class BrokenBarrierError(Exception):
    """Raised when a barrier generation is broken by a timeout."""


class FairBarrier:
    """Cyclic barrier for a fixed number of parties.

    Each generation is released exactly once, and only after all
    ``parties`` threads have called wait(). wait() returns the arrival
    index (0 for the first arrival of the generation), so arrival order
    is observable and no thread can be released twice per generation.

    A wait() timeout breaks the current generation: every waiter of
    that generation raises BrokenBarrierError, and the barrier resets
    itself for the next generation.
    """

    def __init__(self, parties: int) -> None:
        if not isinstance(parties, int) or parties < 1:
            raise ValueError("barrier parties must be an integer >= 1")
        self._parties = parties
        self._cond = threading.Condition()
        self._count = 0            # arrivals in the current generation
        self._generation = 0       # monotonically increasing
        self._broken_gen = -1      # generation broken by a timeout, if any

    @property
    def parties(self) -> int:
        return self._parties

    @property
    def n_waiting(self) -> int:
        with self._cond:
            return self._count

    @property
    def generation(self) -> int:
        with self._cond:
            return self._generation

    def wait(self, timeout: float | None = None) -> int:
        """Wait for all parties. Returns this thread's arrival index."""
        with self._cond:
            gen = self._generation
            index = self._count
            self._count += 1

            if self._count == self._parties:
                # Last arrival releases the generation exactly once.
                self._generation += 1
                self._count = 0
                self._cond.notify_all()
                return index

            deadline = None if timeout is None else (
                time.monotonic() + max(timeout, 0.0)
            )
            while self._generation == gen:
                if deadline is None:
                    self._cond.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    # Break this generation; everyone waiting on it fails,
                    # and the barrier is immediately reusable.
                    self._broken_gen = gen
                    self._generation += 1
                    self._count = 0
                    self._cond.notify_all()
                    raise BrokenBarrierError(
                        "barrier wait timed out; generation broken"
                    )
                self._cond.wait(remaining)

            if gen == self._broken_gen:
                raise BrokenBarrierError("barrier generation was broken")
            return index

    def __repr__(self) -> str:
        with self._cond:
            return (
                f"FairBarrier(parties={self._parties}, "
                f"waiting={self._count}, generation={self._generation})"
            )
