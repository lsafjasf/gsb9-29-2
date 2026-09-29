"""coop_sched: 单线程协作式协程调度器（仅标准库）。

协程即生成器：
    yield                -> 让步（yield_now）
    yield Sleep(seconds) -> 睡眠指定秒数
    sched.wake(task)     -> 唤醒任务（睡眠前/睡眠中调用均不丢失）
    sched.spawn(fn, ...) -> 创建任务
    sched.run()          -> 运行事件循环，返回统计信息

防饥饿：每次恢复协程时测量其连续执行时长，超过阈值即记录并报告。
"""

from __future__ import annotations

import heapq
import itertools
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Generator, Optional

__all__ = ["Sleep", "Task", "Scheduler", "Stats", "RoundStat", "StarvationEvent"]


class Sleep:
    """协程通过 `yield Sleep(seconds)` 请求的睡眠原语。"""

    __slots__ = ("delay",)

    def __init__(self, delay: float):
        if delay < 0:
            raise ValueError("sleep delay must be >= 0")
        self.delay = float(delay)

    def __repr__(self) -> str:  # pragma: no cover
        return f"Sleep({self.delay})"


@dataclass
class StarvationEvent:
    """一次"长时间不让步"事件。"""

    task_name: str
    round_no: int
    duration: float

    def __str__(self) -> str:
        return (
            f"[starvation] task={self.task_name!r} round={self.round_no} "
            f"ran {self.duration * 1000:.1f}ms without yielding"
        )


@dataclass
class RoundStat:
    """事件循环单轮统计。"""

    round_no: int
    tasks_run: int          # 本轮恢复执行的协程数
    duration: float         # 本轮墙钟耗时（秒）


@dataclass
class Stats:
    """整个调度过程的统计。"""

    spawned: int = 0
    finished: int = 0
    steps: int = 0               # 协程被恢复执行的总次数
    rounds: list = field(default_factory=list)        # list[RoundStat]
    wake_calls: int = 0          # wake() 调用总次数
    wakeups_delivered: int = 0   # 实际生效的唤醒次数
    wake_after_done: int = 0     # 唤醒已结束协程的次数（安全 no-op）
    starvation_events: list = field(default_factory=list)  # list[StarvationEvent]
    wall_time: float = 0.0

    def summary(self) -> str:
        lines = [
            "=== scheduler stats ===",
            f"tasks spawned/finished : {self.spawned}/{self.finished}",
            f"total steps (resumes)  : {self.steps}",
            f"event-loop rounds      : {len(self.rounds)}",
            f"wake calls             : {self.wake_calls}",
            f"wakeups delivered      : {self.wakeups_delivered}",
            f"wake after done (no-op): {self.wake_after_done}",
            f"starvation events      : {len(self.starvation_events)}",
            f"wall time              : {self.wall_time * 1000:.1f}ms",
        ]
        if self.rounds:
            lines.append("--- per-round execution ---")
            for r in self.rounds:
                lines.append(
                    f"round {r.round_no:>4}: ran {r.tasks_run:>6} task(s) "
                    f"in {r.duration * 1000:8.3f}ms"
                )
        if self.starvation_events:
            lines.append("--- starvation report ---")
            for ev in self.starvation_events:
                lines.append(str(ev))
        return "\n".join(lines)


class Task:
    """一个被调度的协程。"""

    _ids = itertools.count()

    def __init__(self, sched: "Scheduler", gen: Generator, name: Optional[str] = None):
        self.sched = sched
        self.gen = gen
        self.name = name or f"task-{next(Task._ids)}"
        self.state = "ready"          # ready | running | sleeping | done
        self.wake_pending = False     # 睡眠前到达的唤醒在此登记，保证不丢
        self.result: Any = None
        self.steps = 0                # 该任务被恢复执行的次数
        self.run_time = 0.0           # 该任务累计占用 CPU 的时长

    @property
    def done(self) -> bool:
        return self.state == "done"

    def wake(self) -> bool:
        return self.sched.wake(self)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Task {self.name} state={self.state}>"


class Scheduler:
    """协作式事件循环：按就绪队列（FIFO，逐轮快照）公平调度。"""

    def __init__(
        self,
        starvation_threshold: float = 0.05,
        on_starvation: Optional[Callable[[StarvationEvent], None]] = None,
    ):
        self.starvation_threshold = float(starvation_threshold)
        self._on_starvation = on_starvation or self._default_starvation_reporter
        self._ready: deque = deque()
        self._sleeping: list = []     # heap of (deadline, seq, Task)
        self._seq = itertools.count() # 堆元素次序，避免比较 Task
        self.stats = Stats()

    # ------------------------------------------------------------------ API

    def spawn(self, fn: Callable, *args, name: Optional[str] = None) -> Task:
        """创建任务。fn 须为生成器函数。"""
        gen = fn(*args)
        if not hasattr(gen, "send"):
            raise TypeError(f"{fn!r} did not produce a generator/coroutine")
        task = Task(self, gen, name=name)
        self._ready.append(task)
        self.stats.spawned += 1
        return task

    def wake(self, task: Task) -> bool:
        """唤醒任务。

        - 任务睡眠中：立即移入就绪队列；
        - 任务尚未睡眠（就绪/运行中）：登记 wake_pending，
          其随后的 sleep 会被立即消费 —— 唤醒不丢；
        - 任务已结束：安全 no-op。
        """
        self.stats.wake_calls += 1
        if task.state == "done":
            self.stats.wake_after_done += 1
            return False
        if task.state == "sleeping":
            task.state = "ready"
            task.wake_pending = False
            self._ready.append(task)
            self.stats.wakeups_delivered += 1
            return True
        task.wake_pending = True
        return True

    def run(self) -> Stats:
        """运行事件循环直到所有任务结束，返回统计信息。"""
        self.stats = Stats()
        self.stats.spawned = sum(1 for t in self._ready)
        start_all = time.monotonic()
        round_no = 0
        while self._ready or self._sleeping:
            if not self._ready:
                self._promote_due_sleepers()
                continue
            round_no += 1
            round_start = time.monotonic()
            # 逐轮快照：本轮只执行当前就绪的任务，
            # 新就绪的任务排到下一轮，保证公平、防饥饿。
            n = len(self._ready)
            ran = 0
            for _ in range(n):
                task = self._ready.popleft()
                if task.state != "ready":
                    continue
                self._resume(task, round_no)
                ran += 1
            self.stats.rounds.append(
                RoundStat(round_no, ran, time.monotonic() - round_start)
            )
        self.stats.wall_time = time.monotonic() - start_all
        return self.stats

    # ------------------------------------------------------------- internal

    def _resume(self, task: Task, round_no: int) -> None:
        task.state = "running"
        task.steps += 1
        self.stats.steps += 1
        start = time.monotonic()
        try:
            request = task.gen.send(None)
        except StopIteration as stop:
            task.state = "done"
            task.result = stop.value
            task.wake_pending = False
            self.stats.finished += 1
        else:
            if isinstance(request, Sleep):
                if task.wake_pending:
                    # 睡眠之前已有唤醒到达：立即消费，不进入睡眠 —— 唤醒不丢。
                    task.wake_pending = False
                    task.state = "ready"
                    self._ready.append(task)
                    self.stats.wakeups_delivered += 1
                else:
                    task.state = "sleeping"
                    deadline = time.monotonic() + request.delay
                    heapq.heappush(
                        self._sleeping, (deadline, next(self._seq), task)
                    )
            else:
                # 普通 yield：让步，排到本队列尾部（下一轮执行）。
                task.state = "ready"
                self._ready.append(task)
        finally:
            duration = time.monotonic() - start
            task.run_time += duration
            if duration > self.starvation_threshold:
                event = StarvationEvent(task.name, round_no, duration)
                self.stats.starvation_events.append(event)
                self._on_starvation(event)

    def _promote_due_sleepers(self) -> None:
        """就绪队列为空时，把到期的睡眠任务迁入就绪队列。"""
        while True:
            now = time.monotonic()
            promoted = False
            while self._sleeping:
                deadline, _, task = self._sleeping[0]
                if task.state != "sleeping":
                    heapq.heappop(self._sleeping)  # 已被 wake 提前搬走，丢弃残项
                    continue
                if deadline > now:
                    break
                heapq.heappop(self._sleeping)
                task.state = "ready"
                self._ready.append(task)
                promoted = True
            if promoted or not self._sleeping:
                return
            # 没有就绪任务：真的睡到最近截止时刻（真实事件循环行为）。
            time.sleep(min(self._sleeping[0][0] - time.monotonic(), 0.1))

    @staticmethod
    def _default_starvation_reporter(event: StarvationEvent) -> None:
        print(str(event), file=sys.stderr)
