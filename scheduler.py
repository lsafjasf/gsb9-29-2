"""Cooperative single-threaded coroutine scheduler (stdlib only).

Coroutines are generators. They cooperate by yielding commands:

    yield                 -> yield control, requeue at the back of the ready queue
    yield sleep(seconds)  -> sleep, get woken by the timer heap
    yield wait(event)     -> block until event.set(); a set() that happened
                             before the wait still counts (no lost wakeups)

The scheduler runs a ready queue in rounds, measures how long each coroutine
runs per step, and reports any step exceeding `max_run_time` as a
starvation violation. If only blocked coroutines remain with no timers
pending, it stops and reports them as deadlocked (permanently asleep).
"""

import heapq
import time
from collections import deque


class Sleep:
    __slots__ = ("delay",)

    def __init__(self, delay):
        self.delay = delay


class WaitEvent:
    __slots__ = ("event",)

    def __init__(self, event):
        self.event = event


def sleep(delay):
    return Sleep(delay)


def wait(event):
    return WaitEvent(event)


class Event:
    """Set-before-wait is remembered: a later wait() passes immediately."""

    def __init__(self, scheduler):
        self._scheduler = scheduler
        self._flag = False
        self._waiters = []

    def set(self):
        self._flag = True
        waiters, self._waiters = self._waiters, []
        for task in waiters:
            if task.state == "waiting" and task._waiting_event is self:
                task._waiting_event = None
                task.state = "ready"
                self._scheduler._ready.append(task)

    def clear(self):
        self._flag = False

    def is_set(self):
        return self._flag


class Task:
    def __init__(self, gen, name=None):
        self.gen = gen
        self.name = name or getattr(gen, "__name__", None) or repr(gen)
        self.state = "ready"  # ready | running | sleeping | waiting | done
        self.finished = False
        self.result = None
        self.steps = 0
        self.total_run_time = 0.0
        self._timer_token = 0
        self._waiting_event = None


class Scheduler:
    def __init__(self, max_run_time=0.05, time_source=time.monotonic):
        self.max_run_time = max_run_time
        self._now = time_source
        self._ready = deque()
        self._timers = []  # heap of (wake_at, seq, token, task)
        self._timer_seq = 0
        self.tasks = []
        self.round_task_counts = []
        self.violations = []
        self.switches = 0
        self.deadlocked = []

    def create_task(self, gen, name=None):
        task = Task(gen, name)
        self.tasks.append(task)
        self._ready.append(task)
        return task

    def event(self):
        return Event(self)

    def wake_task(self, task):
        """Wake a sleeping/waiting task. No-op (returns False) for tasks
        that are finished, already ready, or running."""
        if task.state in ("done", "ready", "running"):
            return False
        if task.state == "sleeping":
            task._timer_token += 1  # lazily cancel the pending timer entry
        elif task.state == "waiting":
            task._waiting_event._waiters.remove(task)
            task._waiting_event = None
        task.state = "ready"
        self._ready.append(task)
        return True

    def run(self):
        while True:
            self._expire_timers()
            if not self._ready:
                if self._timers:
                    self._sleep_until_next_timer()
                    continue
                break
            count = 0
            for _ in range(len(self._ready)):
                task = self._ready.popleft()
                if task.state != "ready":
                    continue
                self._step(task)
                count += 1
            self.round_task_counts.append(count)
        self.deadlocked = [t for t in self.tasks if not t.finished]
        return self.stats()

    def _step(self, task):
        task.state = "running"
        start = self._now()
        try:
            cmd = next(task.gen)
        except StopIteration as stop:
            task.finished = True
            task.state = "done"
            task.result = stop.value
            cmd = None
        elapsed = self._now() - start
        task.steps += 1
        task.total_run_time += elapsed
        self.switches += 1
        if elapsed > self.max_run_time:
            self.violations.append({
                "task": task.name,
                "run_time": elapsed,
                "round": len(self.round_task_counts),
            })
        if not task.finished:
            self._handle(task, cmd)

    def _handle(self, task, cmd):
        if cmd is None:
            task.state = "ready"
            self._ready.append(task)
        elif isinstance(cmd, Sleep):
            task.state = "sleeping"
            task._timer_token += 1
            self._timer_seq += 1
            heapq.heappush(
                self._timers,
                (self._now() + cmd.delay, self._timer_seq, task._timer_token, task),
            )
        elif isinstance(cmd, WaitEvent):
            event = cmd.event
            if event.is_set():
                task.state = "ready"
                self._ready.append(task)
            else:
                task.state = "waiting"
                task._waiting_event = event
                event._waiters.append(task)
        else:
            raise TypeError(f"unknown command yielded by {task.name}: {cmd!r}")

    def _expire_timers(self):
        now = self._now()
        while self._timers and self._timers[0][0] <= now:
            _, _, token, task = heapq.heappop(self._timers)
            if task.state == "sleeping" and token == task._timer_token:
                task.state = "ready"
                self._ready.append(task)

    def _sleep_until_next_timer(self):
        while self._timers:
            wake_at, _, token, task = self._timers[0]
            if task.state == "sleeping" and token == task._timer_token:
                delay = wake_at - self._now()
                if delay > 0:
                    time.sleep(delay)
                return
            heapq.heappop(self._timers)

    def stats(self):
        return {
            "tasks_total": len(self.tasks),
            "tasks_finished": sum(1 for t in self.tasks if t.finished),
            "rounds": len(self.round_task_counts),
            "round_task_counts": list(self.round_task_counts),
            "context_switches": self.switches,
            "violations": list(self.violations),
            "deadlocked": [t.name for t in self.deadlocked],
        }
