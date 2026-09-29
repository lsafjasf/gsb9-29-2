"""The *buggy* scheduler used to reproduce the starvation report.

Policy (a textbook strict-priority run-to-block scheduler):

* Always dispatch the ready task with the smallest static priority number.
* A dispatched task runs until it blocks on a lock, finishes, or explicitly
  yields. ``Work`` bursts are executed **without any preemption**, no matter
  how long they are.
* Locks are plain mutexes: no priority inheritance.

Two well-known pathologies follow directly:

1. **Starvation** - a high-priority task with a long ``Work`` burst hogs the
   CPU; every low-priority task sits in the ready queue until the burst is
   over (i.e. "only when load comes down do they run").
2. **Priority inversion** - a low-priority lock holder that yields while
   holding the lock is overtaken by medium-priority CPU hogs, so a blocked
   high-priority waiter is delayed by the *entire* medium workload.

Same event API as :mod:`scheduler`, so identical task bodies run on both.
Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Set

from .scheduler import Acquire, DeadlockError, Lock, Release, Work, Yield

__all__ = ["NaiveScheduler"]


@dataclass(eq=False)
class _NaiveTask:
    name: str
    priority: int
    gen: Iterator[object]
    seq: int
    finished: bool = False
    first_start: Optional[int] = None
    last_start: Optional[int] = None


class NaiveScheduler:
    def __init__(self) -> None:
        self.tasks: Dict[str, _NaiveTask] = {}
        self.ready: List[_NaiveTask] = []
        self.blocked: Set[_NaiveTask] = set()
        self.locks: List[Lock] = []
        self.now: int = 0
        self._seq = 0
        self.trace: List[tuple] = []

    def spawn(self, name: str, priority: int, gen: Iterator[object]) -> _NaiveTask:
        if name in self.tasks:
            raise ValueError(f"duplicate task name: {name!r}")
        task = _NaiveTask(name, priority, gen, self._seq)
        self._seq += 1
        self.tasks[name] = task
        self.ready.append(task)
        return task

    def new_lock(self, name: str) -> Lock:
        lk = Lock(name)
        self.locks.append(lk)
        return lk

    def run(self) -> None:
        while self.ready or self.blocked:
            task = min(self.ready, key=lambda t: (t.priority, t.seq))
            self.ready.remove(task)
            self._dispatch(task)
        # Defensive: all blocked with no ready is detected inside dispatch;
        # an empty spawn set simply terminates.

    def _dispatch(self, task: _NaiveTask) -> None:
        if task.first_start is None:
            task.first_start = self.now
        task.last_start = self.now

        while True:
            try:
                event = next(task.gen)
            except StopIteration:
                task.finished = True
                self.trace.append(("done", task.name))
                return

            if isinstance(event, Work):
                start = self.now
                self.now += event.ticks  # <-- never preempted
                self.trace.append(("run", task.name, start, self.now))
                continue

            if isinstance(event, Yield):
                self.ready.append(task)
                return

            if isinstance(event, Acquire):
                lk = event.lock
                if lk.holder is None:
                    lk.holder = task
                    continue
                lk.waiters.append(task)
                self.blocked.add(task)
                self.trace.append(("block", task.name, lk.name))
                if not self.ready and self.blocked:
                    raise DeadlockError(
                        "ready queue empty with blocked tasks"
                    )
                return

            if isinstance(event, Release):
                lk = event.lock
                if lk.holder is not task:
                    raise RuntimeError(
                        f"task {task.name!r} released lock {lk.name!r} it does not hold"
                    )
                if lk.waiters:
                    nxt = lk.waiters.pop(0)
                    lk.holder = nxt
                    self.blocked.discard(nxt)
                    self.ready.append(nxt)
                    self.trace.append(("wake", nxt.name, lk.name))
                else:
                    lk.holder = None
                continue

            raise TypeError(f"unknown event: {event!r}")
