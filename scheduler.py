"""Cooperative coroutine scheduler with priority aging (starvation-free).

Model
-----
A coroutine is a Python generator; each ``yield`` is one unit of work (a
"step").  The scheduler is cooperative: it dispatches one ready coroutine at
a time, lets it run for at most ``time_slice`` steps (the per-round execution
quantum), then requeues it if it has not finished.

Scheduling rule (deterministic)
-------------------------------
Each ready coroutine has a base priority ``p`` in ``[0, num_priorities)``
(higher = more urgent) and an effective priority

    eff = p + wait // aging_interval

where ``wait`` is the number of dispatches since the coroutine was last
dispatched (aging).  The scheduler always dispatches the ready coroutine
with the highest effective priority; ties are broken by the oldest enqueue
sequence number (FIFO).  Given the same spawn order and priorities, the
execution sequence is therefore uniquely determined and reproducible.

Wait bound (derivation in BOUND.md)
-----------------------------------
A ready coroutine with base priority ``p`` waits at most

    W(p)       = aging_interval * (num_priorities - p) + (N - 1)   dispatches
    W_steps(p) = time_slice * W(p)                               steps

where N is the maximum ready-queue length during its wait.
"""

from collections import deque


class Task:
    __slots__ = (
        "tid", "name", "priority", "gen", "wait", "seq", "done",
        "steps", "first_dispatch", "max_wait", "max_wait_steps",
        "_streak_step_mark",
    )

    def __init__(self, tid, name, priority, gen, seq):
        self.tid = tid
        self.name = name
        self.priority = priority
        self.gen = gen
        self.wait = 0               # dispatches since last dispatch (aging)
        self.seq = seq             # enqueue sequence number (FIFO tie-break)
        self.done = False
        self.steps = 0             # steps executed so far
        self.first_dispatch = None  # dispatch clock of first dispatch
        self.max_wait = 0          # longest completed wait streak (dispatches)
        self.max_wait_steps = 0    # longest completed wait streak (steps)
        self._streak_step_mark = 0


class Scheduler:
    def __init__(self, num_priorities=8, aging_interval=4, time_slice=1):
        if num_priorities < 1:
            raise ValueError("num_priorities must be >= 1")
        if aging_interval < 1:
            raise ValueError("aging_interval must be >= 1")
        if time_slice < 1:
            raise ValueError("time_slice must be >= 1")
        self.num_priorities = num_priorities
        self.aging_interval = aging_interval
        self.time_slice = time_slice
        self._tasks = {}
        self._ready = deque()
        self._seq = 0
        self._next_tid = 0
        self.now = 0           # dispatch clock
        self.total_steps = 0   # steps executed across all tasks
        self.max_ready = 0     # high-water mark of the ready queue
        self.log = []          # tids in dispatch order

    # -- API ----------------------------------------------------------------
    def spawn(self, gen, priority=0, name=None):
        if not 0 <= priority < self.num_priorities:
            raise ValueError("priority out of range [0, %d)" % self.num_priorities)
        tid = self._next_tid
        self._next_tid += 1
        task = Task(tid, name or "task-%d" % tid, priority, gen, self._seq)
        self._seq += 1
        task._streak_step_mark = self.total_steps
        self._tasks[tid] = task
        self._ready.append(tid)
        if len(self._ready) > self.max_ready:
            self.max_ready = len(self._ready)
        return tid

    def effective_priority(self, task):
        return task.priority + task.wait // self.aging_interval

    def wait_bound(self, priority, n_ready=None):
        """Upper bound on the wait (in dispatches) for base ``priority``."""
        n = self.max_ready if n_ready is None else n_ready
        return self.aging_interval * (self.num_priorities - priority) + max(n - 1, 0)

    def task(self, tid):
        return self._tasks[tid]

    def __len__(self):
        return len(self._ready)

    # -- core loop ----------------------------------------------------------
    def _pick(self):
        best_tid, best_key = None, None
        for tid in self._ready:
            task = self._tasks[tid]
            key = (self.effective_priority(task), -task.seq)
            if best_key is None or key > best_key:
                best_tid, best_key = tid, key
        return best_tid

    def step(self):
        """Dispatch one coroutine for up to ``time_slice`` steps.

        Returns the dispatched tid, or None when the ready queue is empty.
        """
        if not self._ready:
            return None
        tid = self._pick()
        self._ready.remove(tid)
        task = self._tasks[tid]
        ran = 0
        while ran < self.time_slice and not task.done:
            try:
                next(task.gen)
                ran += 1
            except StopIteration:
                task.done = True
        task.steps += ran
        self.total_steps += ran
        if task.first_dispatch is None:
            task.first_dispatch = self.now
        if task.wait > task.max_wait:
            task.max_wait = task.wait
        wait_steps = self.total_steps - task._streak_step_mark
        if wait_steps > task.max_wait_steps:
            task.max_wait_steps = wait_steps
        for other_tid in self._ready:
            self._tasks[other_tid].wait += 1
        self.now += 1
        self.log.append(tid)
        if not task.done:
            task.wait = 0
            task.seq = self._seq
            self._seq += 1
            task._streak_step_mark = self.total_steps
            self._ready.append(tid)
        return tid

    def run(self, max_dispatches=None):
        """Run until the ready queue is empty (or max_dispatches reached).

        Returns the dispatch log (tids in execution order).
        """
        dispatches = 0
        while self._ready and (max_dispatches is None or dispatches < max_dispatches):
            self.step()
            dispatches += 1
        return list(self.log)
