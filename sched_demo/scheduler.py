"""Deterministic, starvation-free coroutine scheduler (simulated time).

Task model
----------
A task is a Python generator yielding events:

    Work(ticks)   occupy the (simulated) CPU for `ticks` ticks
    Yield()       voluntarily give up the rest of the current round
    Acquire(lock) take a binary mutex (blocks while it is held)
    Release(lock) release a binary mutex (FIFO wake-up, next round)

Scheduling rule
---------------
Time is divided into rounds. At the start of every round the ready queue is
snapshotted and sorted by ``(effective_priority, spawn_sequence)``; each
ready task is then dispatched in that order with the *same fixed budget* of
``quantum`` simulated ticks. A long ``Work`` burst that exceeds the budget
is preempted and continued in the following round, so a task that never
voluntarily yields cannot hold the CPU for more than ``quantum`` ticks at a
time.

Consequences (see README_zh.md for the proofs):

* Every ready task is revisited at least once per round  -> no starvation.
* Waiting time inside a round <= ``(n - 1) * quantum`` where ``n`` is the
  number of ready tasks snapshotted for that round.
* Same-priority tasks run FIFO and priority only affects position inside a
  round, so the execution trace is a deterministic function of spawn order
  and static priorities.
* Mutexes use priority inheritance: a lock holder is boosted to the
  priority of the highest-priority waiter (transitively), bounding the
  classic priority inversion independently of medium-priority load.

Only the Python standard library is used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Set, Tuple

__all__ = [
    "Work",
    "Yield",
    "Acquire",
    "Release",
    "Task",
    "Lock",
    "Scheduler",
    "DeadlockError",
]


# --------------------------------------------------------------------------- #
# Events yielded by task generators
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Work:
    """Occupy the CPU for `ticks` ticks of simulated time."""

    ticks: int

    def __post_init__(self) -> None:
        if self.ticks < 0:
            raise ValueError("Work.ticks must be non-negative")


@dataclass(frozen=True)
class Yield:
    """Voluntarily give up the rest of the current round."""


@dataclass(frozen=True)
class Acquire:
    lock: "Lock"


@dataclass(frozen=True)
class Release:
    lock: "Lock"


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #


@dataclass(eq=False)
class Task:
    name: str
    priority: int  # smaller number == higher priority
    gen: Iterator[object]
    seq: int  # global spawn sequence; FIFO tie-breaker

    finished: bool = False
    pending_work: int = 0  # remainder of a preempted Work burst
    first_start: Optional[int] = None
    last_start: Optional[int] = None
    # one entry per dispatch: (time the task became ready for this slice,
    # round start, dispatch time)
    waits: List[Tuple[int, int, int]] = field(default_factory=list)
    # timestamp of the latest enqueue/wake; None while not in ready queue
    ready_since: Optional[int] = None

    def record_start(self, now: int, round_started_at: int) -> None:
        if self.first_start is None:
            self.first_start = now
        self.last_start = now
        ready_since = self.ready_since if self.ready_since is not None else round_started_at
        self.waits.append((ready_since, round_started_at, now))
        self.ready_since = None


class DeadlockError(RuntimeError):
    """No task is ready and at least one task is blocked forever."""


class Lock:
    """Binary mutex with a FIFO waiter queue."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.holder: Optional[Task] = None
        self.waiters: List[Task] = []

    def acquire(self) -> Acquire:
        return Acquire(self)

    def release(self) -> Release:
        return Release(self)


# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #


class Scheduler:
    """Round-based priority round-robin with a fixed per-round quantum."""

    def __init__(self, quantum: int = 10, inherit: bool = True) -> None:
        if quantum <= 0:
            raise ValueError("quantum must be positive")
        self.quantum = quantum
        self.inherit = inherit

        self.tasks: Dict[str, Task] = {}
        self.ready: List[Task] = []
        self.blocked: Set[Task] = set()
        self.locks: List[Lock] = []

        self.now: int = 0
        self.round_no: int = 0
        self._seq: int = 0
        self._running: bool = False

        # Deterministic audit trace:
        #   ("round", round_no, time)
        #   ("run",   round_no, task_name, start, end)
        #   ("block", task_name, lock_name)
        #   ("wake",  task_name, lock_name)
        #   ("done",  task_name)
        self.trace: List[tuple] = []

    # ------------------------------ public ------------------------------ #

    def spawn(self, name: str, priority: int, gen: Iterator[object]) -> Task:
        if name in self.tasks:
            raise ValueError(f"duplicate task name: {name!r}")
        task = Task(name=name, priority=priority, gen=gen, seq=self._seq)
        self._seq += 1
        task.ready_since = self.now
        self.tasks[name] = task
        self.ready.append(task)
        return task

    def new_lock(self, name: str) -> Lock:
        lk = Lock(name)
        self.locks.append(lk)
        return lk

    def run(self) -> None:
        """Advance simulated time until every spawned task has finished."""
        if self._running:
            raise RuntimeError("scheduler is already running")
        self._running = True
        try:
            while self.ready or self.blocked:
                self._run_round()
        finally:
            self._running = False

    def max_wait(self) -> int:
        """Largest ready->dispatch delay observed so far (simulated ticks)."""
        return max(
            (
                dispatch - ready
                for t in self.tasks.values()
                for ready, _round_start, dispatch in t.waits
            ),
            default=0,
        )

    # ----------------------------- the round ---------------------------- #

    def _run_round(self) -> None:
        self.round_no += 1
        round_started_at = self.now
        self.trace.append(("round", self.round_no, round_started_at))

        # Snapshot: tasks woken by a lock release during this round join the
        # NEXT round. This makes "n = size of the ready snapshot" the exact
        # quantity used in the waiting-time bound.
        plan = self._sort_ready(self.ready)
        self.ready = []

        for task in plan:
            self._dispatch(task, round_started_at)

        if not self.ready and self.blocked:
            names = ", ".join(sorted(t.name for t in self.blocked))
            raise DeadlockError(f"ready queue empty with blocked tasks: {names}")

    def _sort_ready(self, tasks: List[Task]) -> List[Task]:
        # Effective priority is recomputed at round boundaries; spawn
        # sequence is the unique tie-breaker -> total, deterministic order.
        return sorted(tasks, key=lambda t: (self._eff_priority(t), t.seq))

    # ----------------------- priority inheritance ----------------------- #

    def _eff_priority(self, task: Task) -> int:
        """Priority with transitive inheritance along holder <- waiter edges.

        A holder inherits the best (smallest) static priority found among
        tasks transitively blocked on a lock it holds. ``stack`` guards
        against lock cycles; tasks on a cycle are all blocked, so the value
        returned there never influences a dispatch decision.
        """
        if not self.inherit:
            return task.priority

        best = task.priority
        stack: Set[Task] = {task}

        def visit(holder: Task) -> int:
            nonlocal best
            for lk in self.locks:  # creation order -> deterministic walk
                if lk.holder is holder:
                    for waiter in lk.waiters:  # FIFO order
                        if waiter in stack:
                            continue
                        stack.add(waiter)
                        best = min(best, waiter.priority, visit(waiter))
                        stack.discard(waiter)
            return best

        return visit(task)

    # --------------------------- one time slice ------------------------- #

    def _dispatch(self, task: Task, round_started_at: int) -> None:
        """Run `task` for at most `quantum` ticks."""
        budget = self.quantum
        task.record_start(self.now, round_started_at)
        self.trace.append(("dispatch", self.round_no, task.name, self.now))
        blocked_on: Optional[Lock] = None

        while budget > 0:
            # Finish a burst that was preempted in a previous round first.
            if task.pending_work > 0:
                burst = task.pending_work
                event = None
            else:
                try:
                    event = next(task.gen)
                except StopIteration:
                    task.finished = True
                    self.trace.append(("done", task.name))
                    return

                if isinstance(event, Work):
                    burst = event.ticks
                else:
                    burst = 0

                    if isinstance(event, Yield):
                        break

                    if isinstance(event, Acquire):
                        lk = event.lock
                        if lk.holder is None:
                            lk.holder = task
                            continue
                        lk.waiters.append(task)
                        self.blocked.add(task)
                        blocked_on = lk
                        self.trace.append(("block", task.name, lk.name))
                        break

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
                            self.ready.append(nxt)  # runs in the next round
                            self.trace.append(("wake", nxt.name, lk.name))
                        else:
                            lk.holder = None
                        continue

                    raise TypeError(
                        f"unknown event yielded by task {task.name!r}: {event!r}"
                    )

            # CPU burst section (fresh Work or a resumed preemption).
            used = min(budget, burst)
            start = self.now
            self.now += used
            self.trace.append(("run", self.round_no, task.name, start, self.now))
            budget -= used
            if used < burst:
                task.pending_work = burst - used
                task.ready_since = self.now
                self.ready.append(task)
                return
            task.pending_work = 0

        # Exhausted the quantum, yielded, or blocked.
        if blocked_on is None and not task.finished:
            task.ready_since = self.now
            self.ready.append(task)
