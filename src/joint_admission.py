"""Joint (vector) admission control over multiple resources.

A task's demand is a vector, e.g. {"cpu": 2, "memory": 4, "handles": 8}.
The joint controller grants the *whole vector atomically* (all-or-nothing):
a queued task holds nothing, a running task waits for nothing.  This removes
the "hold and wait" Coffman condition, so deadlock is impossible by
construction (see README.md for the full argument).

Also included: PerResourceController, a baseline that admits/acquires each
resource independently (partial holding while waiting), used for comparison.

Standard library only.
"""

from collections import deque

GRANTED = "granted"
QUEUED = "queued"
REJECTED = "rejected"


def _validate(demand, capacity):
    for r, v in demand.items():
        if r not in capacity:
            raise KeyError("unknown resource: %r" % r)
        if v < 0:
            raise ValueError("negative demand for %r" % r)


class Metrics:
    """Time-weighted utilization, queue-wait and rejection statistics."""

    def __init__(self, capacity):
        self.capacity = dict(capacity)
        self.used = {r: 0 for r in capacity}
        self._area = {r: 0.0 for r in capacity}
        self._last_t = 0.0
        self.waits = []          # queue wait of every granted task
        self.granted = 0
        self.rejected = 0
        self.deadlocks = 0       # baseline only: broken wait cycles
        self.completed = 0

    def tick(self, now):
        if now < self._last_t:
            raise ValueError("time went backwards")
        dt = now - self._last_t
        for r in self.used:
            self._area[r] += self.used[r] * dt
        self._last_t = now

    def utilization(self, now=None):
        self.tick(self._last_t if now is None else now)
        out = {}
        for r, cap in self.capacity.items():
            horizon = self._last_t
            out[r] = (self._area[r] / (cap * horizon)) if cap and horizon else 0.0
        return out

    def rejection_rate(self):
        total = self.granted + self.rejected
        return (self.rejected / total) if total else 0.0

    def wait_stats(self):
        if not self.waits:
            return {"avg": 0.0, "p95": 0.0, "max": 0.0}
        ws = sorted(self.waits)
        p95 = ws[min(len(ws) - 1, int(0.95 * (len(ws) - 1) + 0.5))]
        return {"avg": sum(ws) / len(ws), "p95": p95, "max": ws[-1]}


class AdmissionController:
    """Joint vector admission: atomic all-or-nothing grant + FIFO/aging queue.

    Anti-starvation: the queue is FIFO.  A queued task that does not fit may
    be skipped by later (smaller) tasks only while it is younger than
    ``aging_threshold``.  Once aged, it becomes a hard head-of-line: no later
    task is granted before it, so resources can only accumulate towards it
    and it is granted within ``aging_threshold + max_task_duration``.

    Optional ``max_wait`` rejects tasks that waited too long (hard bound on
    queueing time); keep ``aging_threshold < max_wait`` so aging fires first.
    """

    def __init__(self, capacity, aging_threshold=float("inf"), max_wait=None):
        self.capacity = dict(capacity)
        self.aging_threshold = aging_threshold
        self.max_wait = max_wait
        self.metrics = Metrics(capacity)
        self.allocated = {}   # task_id -> dict(resource -> amount) currently held
        self.queue = deque()  # (task_id, demand, arrival_time)

    # -- helpers ---------------------------------------------------------
    @property
    def used(self):
        return self.metrics.used

    def _fits(self, demand):
        return all(self.used[r] + demand.get(r, 0) <= self.capacity[r]
                   for r in self.capacity)

    def _grant(self, task_id, demand, arrival, now):
        for r, v in demand.items():
            self.used[r] += v
        self.allocated[task_id] = dict(demand)
        self.metrics.granted += 1
        self.metrics.waits.append(now - arrival)

    def _drain(self, now):
        granted = []
        entries = list(self.queue)
        remaining = deque()
        for i, (task_id, demand, arrival) in enumerate(entries):
            if self._fits(demand):
                self._grant(task_id, demand, arrival, now)
                granted.append(task_id)
            elif now - arrival >= self.aging_threshold:
                # Aged task: stop the scan so nothing newer jumps ahead.
                remaining.extend(entries[i:])
                break
            else:
                remaining.append((task_id, demand, arrival))
        self.queue = remaining
        return granted

    def _apply_timeouts(self, now):
        if self.max_wait is None:
            return []
        rejected = []
        kept = deque()
        for entry in self.queue:
            if now - entry[2] > self.max_wait:
                self.metrics.rejected += 1
                rejected.append(entry[0])
            else:
                kept.append(entry)
        self.queue = kept
        return rejected

    # -- public API ------------------------------------------------------
    def request(self, task_id, demand, now=0.0):
        """Returns (status, newly_granted_task_ids)."""
        _validate(demand, self.capacity)
        self.metrics.tick(now)
        self._apply_timeouts(now)
        if any(demand.get(r, 0) > self.capacity[r] for r in self.capacity):
            self.metrics.rejected += 1     # can never fit: reject immediately
            return REJECTED, []
        if not self.queue and self._fits(demand):
            self._grant(task_id, demand, now, now)
            return GRANTED, [task_id]
        self.queue.append((task_id, demand, now))
        return QUEUED, []

    def release(self, task_id, now=0.0, resources=None):
        """Release all (or part, via ``resources``) of a task's allocation.

        Mid-task partial release is supported: the task keeps the remainder.
        Returns task ids newly granted from the queue.
        """
        self.metrics.tick(now)
        held = self.allocated.get(task_id)
        if held is None:
            raise KeyError("task %r holds nothing" % task_id)
        if resources is None:
            resources = dict(held)
        for r, v in resources.items():
            if v > held.get(r, 0):
                raise ValueError("releasing more than held for %r" % r)
            held[r] -= v
            self.used[r] -= v
        if all(v == 0 for v in held.values()):
            del self.allocated[task_id]
            self.metrics.completed += 1
        self._apply_timeouts(now)
        return self._drain(now)


class PerResourceController:
    """Baseline: independent per-resource admission with partial holding.

    Each task acquires its resources one at a time, in a task-specific
    ``order``.  While waiting for the next resource it *keeps* the ones it
    already holds (hold-and-wait), which is exactly what makes the joint
    scheme necessary: circular waits (deadlocks) can and do occur.  Deadlocks
    are detected with a wait-for graph and broken by aborting the youngest
    task in the cycle (counted as deadlock + rejection).
    """

    def __init__(self, capacity):
        self.capacity = dict(capacity)
        self.metrics = Metrics(capacity)
        self.held = {}        # task_id -> {resource: amount} already acquired
        self.demands = {}     # task_id -> full demand vector
        self.orders = {}      # task_id -> acquisition order (list of resources)
        self.arrivals = {}    # task_id -> arrival time
        self.running = set()  # tasks that hold their full vector

    @property
    def used(self):
        return self.metrics.used

    def _next_need(self, task_id):
        held = self.held[task_id]
        for r in self.orders[task_id]:
            if held.get(r, 0) < self.demands[task_id].get(r, 0):
                return r
        return None

    def _wait_for_graph(self):
        """Edges: waiting task -> tasks holding the resource it waits for."""
        graph = {}
        for t in self.held:
            if t in self.running:
                continue
            r = self._next_need(t)
            if r is None:
                continue
            need = self.demands[t].get(r, 0) - self.held[t].get(r, 0)
            if self.used[r] + need <= self.capacity[r]:
                continue  # not actually blocked
            graph[t] = {o for o in self.held
                        if o != t and self.held[o].get(r, 0) > 0}
        return graph

    @staticmethod
    def _find_cycle(graph):
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in graph}
        stack = []

        def dfs(node):
            color[node] = GRAY
            stack.append(node)
            for nxt in graph.get(node, ()):
                if color.get(nxt, BLACK) == GRAY:
                    return stack[stack.index(nxt):]
                if color.get(nxt, BLACK) == WHITE:
                    cycle = dfs(nxt)
                    if cycle:
                        return cycle
            stack.pop()
            color[node] = BLACK
            return None

        for n in graph:
            if color[n] == WHITE:
                cycle = dfs(n)
                if cycle:
                    return cycle
        return None

    def _abort(self, task_id, now):
        for r, v in self.held[task_id].items():
            self.used[r] -= v
        for table in (self.held, self.demands, self.orders, self.arrivals):
            table.pop(task_id, None)
        self.running.discard(task_id)
        self.metrics.deadlocks += 1
        self.metrics.rejected += 1

    def _progress(self, now):
        newly_running = []
        moved = True
        while moved:
            moved = False
            for t in sorted(self.held):
                if t in self.running:
                    continue
                r = self._next_need(t)
                if r is None:
                    self.running.add(t)
                    self.metrics.granted += 1
                    self.metrics.waits.append(now - self.arrivals[t])
                    newly_running.append(t)
                    moved = True
                    continue
                need = self.demands[t].get(r, 0) - self.held[t].get(r, 0)
                if self.used[r] + need <= self.capacity[r]:
                    self.used[r] += need
                    self.held[t][r] = self.held[t].get(r, 0) + need
                    moved = True
        # Deadlock detection & resolution.
        while True:
            cycle = self._find_cycle(self._wait_for_graph())
            if not cycle:
                break
            youngest = max(cycle, key=lambda t: self.arrivals[t])
            self._abort(youngest, now)
            newly_running.extend(self._progress(now))
        return newly_running

    def request(self, task_id, demand, now=0.0, order=None):
        _validate(demand, self.capacity)
        self.metrics.tick(now)
        if any(demand.get(r, 0) > self.capacity[r] for r in self.capacity):
            self.metrics.rejected += 1
            return REJECTED, []
        self.held[task_id] = {}
        self.demands[task_id] = dict(demand)
        self.orders[task_id] = list(order) if order else sorted(demand)
        self.arrivals[task_id] = now
        running = self._progress(now)
        if task_id in self.running:
            return GRANTED, running
        return QUEUED, running

    def release(self, task_id, now=0.0, resources=None):
        self.metrics.tick(now)
        held = self.held.get(task_id)
        if held is None:
            raise KeyError("task %r holds nothing" % task_id)
        if resources is None:
            resources = {r: self.demands[task_id].get(r, 0)
                         for r in self.capacity}
        for r, v in resources.items():
            v = min(v, held.get(r, 0))
            if v:
                held[r] -= v
                self.used[r] -= v
        if task_id in self.running and all(
                held.get(r, 0) == 0 for r in self.capacity):
            self.running.discard(task_id)
            for table in (self.held, self.demands, self.orders,
                          self.arrivals):
                table.pop(task_id, None)
            self.metrics.completed += 1
        return self._progress(now)
