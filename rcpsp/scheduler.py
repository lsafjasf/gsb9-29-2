"""资源受限项目调度 (RCPSP)：关键路径 + 资源可用性。

算法
----
1. 依赖校验（拒绝重复任务、缺失依赖、非正工期、环依赖）。
2. 关键路径分析：正向求 ES/EF，反向求 LS/LF（LFT 优先级）。
3. 串行进度生成方案 (Serial SGS)：
   按 LFT 升序（平局取工期长、ID 小）逐个挑任务，把它放到
   “不早于关键路径最早开始、且每个资源在整个执行窗口都不超限”
   的最早时刻。任务不可拆分，时间为离散整数、区间左闭右开 [start, end)。
4. 输出每个任务的起止时间与各资源占用，并给出关键路径下界、
   资源负载下界及比值。

只使用 Python 标准库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


class SchedulingError(ValueError):
    """调度输入非法（含环依赖、缺失依赖、非法工期等）。"""


@dataclass(frozen=True)
class Task:
    tid: str
    duration: int
    demand: Mapping[str, int] = field(default_factory=dict)
    predecessors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScheduledTask:
    tid: str
    start: int
    end: int
    duration: int
    demand: Mapping[str, int]
    predecessors: tuple[str, ...]


@dataclass(frozen=True)
class ScheduleResult:
    tasks: Mapping[str, ScheduledTask]
    makespan: int
    capacities: Mapping[str, int]
    critical_path_length: int
    critical_path: tuple[str, ...]
    lower_bounds: Mapping[str, object]

    def resource_timeline(self, resource: str) -> list[tuple[int, int, int]]:
        """返回 (起点, 终点, 占用量) 的分段占用曲线。"""
        events: dict[int, int] = {}
        for st in self.tasks.values():
            d = int(st.demand.get(resource, 0))
            if d == 0:
                continue
            events[st.start] = events.get(st.start, 0) + d
            events[st.end] = events.get(st.end, 0) - d
        points = sorted(events)
        timeline: list[tuple[int, int, int]] = []
        usage = 0
        for i, t in enumerate(points):
            if i + 1 < len(points) and t < points[i + 1]:
                timeline.append((t, points[i + 1], usage + events[t]))
            usage += events[t]
        return [(a, b, u) for (a, b, u) in timeline if u > 0]

    def format_table(self) -> str:
        lines = ["task                 start  end  dur  demand"]
        for st in sorted(self.tasks.values(), key=lambda s: (s.start, s.tid)):
            dem = ", ".join(f"{r}:{v}" for r, v in sorted(st.demand.items())) or "-"
            lines.append(f"{st.tid:<20s} {st.start:>5d} {st.end:>4d} {st.end - st.start:>4d}  {dem}")
        return "\n".join(lines)


class Scheduler:
    def __init__(self, capacities: int | Mapping[str, int] = 1):
        if isinstance(capacities, int):
            capacities = {"R": capacities}
        caps: dict[str, int] = {}
        for name, cap in capacities.items():
            if not isinstance(cap, int) or cap <= 0:
                raise SchedulingError(f"资源 {name!r} 上限必须为正整数，得到 {cap!r}")
            caps[str(name)] = cap
        if not caps:
            raise SchedulingError("至少需要一种可更新资源")
        self._capacities = caps
        self._tasks: dict[str, Task] = {}

    @property
    def capacities(self) -> Mapping[str, int]:
        return dict(self._capacities)

    def add_task(
        self,
        tid: str,
        duration: int,
        demand: int | Mapping[str, int] | None = None,
        predecessors: "list[str] | tuple[str, ...] | None" = None,
    ) -> "Scheduler":
        tid = str(tid)
        if tid in self._tasks:
            raise SchedulingError(f"任务 {tid!r} 重复定义")
        if not isinstance(duration, int) or duration <= 0:
            raise SchedulingError(f"任务 {tid!r} 工期必须为正整数，得到 {duration!r}")
        if demand is None:
            demand = {}
        if isinstance(demand, int):
            demand = {"R": demand}
        dem: dict[str, int] = {}
        for resource, amount in demand.items():
            if not isinstance(amount, int) or amount < 0:
                raise SchedulingError(f"任务 {tid!r} 对资源 {resource!r} 的需求非法: {amount!r}")
            if amount > 0:
                dem[str(resource)] = amount
        for resource, amount in dem.items():
            if amount > self._capacities.get(resource, 0):
                raise SchedulingError(
                    f"任务 {tid!r} 需要 {resource}={amount}，超过上限 {self._capacities.get(resource, 0)}"
                )
        preds = tuple(str(p) for p in (predecessors or ()))
        self._tasks[tid] = Task(tid, duration, dem, preds)
        return self

    # ------------------------------------------------------------------ 校验
    def _validate(self) -> None:
        for t in self._tasks.values():
            for p in t.predecessors:
                if p not in self._tasks:
                    raise SchedulingError(f"任务 {t.tid!r} 依赖了不存在的任务 {p!r}")
                if p == t.tid:
                    raise SchedulingError(f"任务 {t.tid!r} 不能依赖自身")

    def _topological_order(self) -> list[str]:
        """Kahn 拓扑排序；存在环时抛出包含环路径的异常。"""
        indeg = {tid: 0 for tid in self._tasks}
        children: dict[str, list[str]] = {tid: [] for tid in self._tasks}
        for t in self._tasks.values():
            for p in t.predecessors:
                indeg[t.tid] += 1
                children[p].append(t.tid)
        ready = sorted(tid for tid, d in indeg.items() if d == 0)
        order: list[str] = []
        while ready:
            cur = ready.pop(0)
            order.append(cur)
            for child in children[cur]:
                indeg[child] -= 1
                if indeg[child] == 0:
                    ready.append(child)
            ready.sort()
        if len(order) != len(self._tasks):
            cycle = self._find_cycle()
            raise SchedulingError(f"依赖图含环，无法调度：{' -> '.join(cycle)}")
        return order

    def _find_cycle(self) -> list[str]:
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {tid: WHITE for tid in self._tasks}
        stack: list[str] = []

        def dfs(node: str) -> list[str] | None:
            color[node] = GRAY
            stack.append(node)
            for nxt in sorted(self._tasks[node].predecessors):
                # 边方向：node 依赖 nxt（nxt 必须先完成），沿依赖方向找环
                if color[nxt] == GRAY:
                    start = stack.index(nxt)
                    return stack[start:] + [nxt]
                if color[nxt] == WHITE:
                    found = dfs(nxt)
                    if found:
                        return found
            stack.pop()
            color[node] = BLACK
            return None

        for tid in sorted(self._tasks):
            if color[tid] == WHITE:
                found = dfs(tid)
                if found:
                    return found
        return []  # 理论不可达

    # ---------------------------------------------------------------- 关键路径
    def critical_path(self) -> tuple[int, tuple[str, ...], dict[str, int], dict[str, int]]:
        """返回 (CP 长度, 路径任务, ES 表, LF 表)。"""
        order = self._topological_order()
        es: dict[str, int] = {}
        pred_on_path: dict[str, str] = {}
        for tid in order:
            t = self._tasks[tid]
            earliest = 0
            best_pred = ""
            for p in t.predecessors:
                if es[p] + self._tasks[p].duration > earliest:
                    earliest = es[p] + self._tasks[p].duration
                    best_pred = p
            es[tid] = earliest
            if best_pred:
                pred_on_path[tid] = best_pred
        cp_len = max((es[t] + self._tasks[t].duration for t in self._tasks), default=0)

        lf: dict[str, int] = {}
        for tid in reversed(order):
            t = self._tasks[tid]
            followers = [c for c in self._tasks if tid in self._tasks[c].predecessors]
            lf[tid] = min((lf[c] - self._tasks[c].duration for c in followers), default=cp_len)

        end_task = max(order, key=lambda x: es[x] + self._tasks[x].duration)
        path = [end_task]
        while path[-1] in pred_on_path:
            path.append(pred_on_path[path[-1]])
        path.reverse()
        return cp_len, tuple(path), es, lf

    # ---------------------------------------------------------------- 下界
    def lower_bounds(self) -> dict[str, object]:
        cp_len, path, _, _ = self.critical_path()
        lbs: dict[str, int] = {}
        for resource, cap in self._capacities.items():
            work = sum(t.duration * int(t.demand.get(resource, 0)) for t in self._tasks.values())
            lbs[resource] = -(-work // cap)  # ceil(work / cap)
        max_single = max(t.duration for t in self._tasks.values())
        overall = max([cp_len, max_single, *lbs.values()])
        return {
            "critical_path": cp_len,
            "critical_path_tasks": path,
            "resource_work_lower_bounds": lbs,
            "longest_task": max_single,
            "overall": overall,
        }

    # ---------------------------------------------------------------- 调度
    def schedule(self) -> ScheduleResult:
        self._validate()
        cp_len, path, es, lf = self.critical_path()

        scheduled: dict[str, ScheduledTask] = {}
        remaining = set(self._tasks)

        while remaining:
            eligible = [
                tid
                for tid in remaining
                if all(p in scheduled for p in self._tasks[tid].predecessors)
            ]
            # LFT 优先：最晚完成越早越关键；平局取工期长、拓扑序早、ID 小
            eligible.sort(
                key=lambda x: (
                    lf[x],
                    -self._tasks[x].duration,
                    es[x],
                    x,
                )
            )
            tid = eligible[0]
            t = self._tasks[tid]
            dep_ready = max(
                (scheduled[p].end for p in t.predecessors),
                default=0,
            )
            earliest = max(dep_ready, es[tid])
            start = self._earliest_feasible(t, earliest, scheduled)
            scheduled[tid] = ScheduledTask(
                tid=tid,
                start=start,
                end=start + t.duration,
                duration=t.duration,
                demand=dict(t.demand),
                predecessors=t.predecessors,
            )
            remaining.remove(tid)

        makespan = max((st.end for st in scheduled.values()), default=0)
        return ScheduleResult(
            tasks=dict(scheduled),
            makespan=makespan,
            capacities=dict(self._capacities),
            critical_path_length=cp_len,
            critical_path=path,
            lower_bounds=self.lower_bounds(),
        )

    def _earliest_feasible(
        self,
        task: Task,
        earliest: int,
        scheduled: Mapping[str, ScheduledTask],
    ) -> int:
        """从 earliest 起，找任务可完整放入的最早整数时刻。

        占用量只在已有任务的起止事件处变化，因此只需在事件点与 earliest
        处尝试；尝试点失败后跳到“第一个导致超限的已排任务结束时刻”。
        """
        end_events = sorted({st.end for st in scheduled.values()})
        candidate = earliest
        while True:
            if self._infeasible_until(task, candidate, scheduled) is None:
                return candidate
            nxt = next((e for e in end_events if e > candidate), None)
            if nxt is None:  # 候选点之后没有任何已排任务结束，之后必然空闲
                return candidate
            candidate = nxt

    def _infeasible_until(
        self,
        task: Task,
        start: int,
        scheduled: Mapping[str, ScheduledTask],
    ) -> int | None:
        """检查 [start, start+duration) 是否违反任一资源上限。

        返回首个超限时间点；完全可行返回 None。
        """
        finish = start + task.duration
        # 收集窗口内所有占用变化事件（含窗口起点基线）
        events: dict[str, dict[int, int]] = {r: {} for r in self._capacities}
        for st in scheduled.values():
            if st.end <= start or st.start >= finish:
                continue
            for r, d in st.demand.items():
                events[r].setdefault(st.start, 0)
                events[r][st.end] = events[r].get(st.end, 0) - d
                events[r][st.start] = events[r].get(st.start, 0) + d
        first_bad: int | None = None
        for resource, cap in self._capacities.items():
            need = int(task.demand.get(resource, 0))
            if need == 0:
                continue
            ev = events[resource]
            # 窗口 [start, finish) 内部的占用变化点（不含 start：
            # start 处的占用已由 baseline 统计，起点事件不能重复加）
            points = sorted(p for p in ev if start < p < finish)
            baseline = sum(
                st.demand.get(resource, 0)
                for st in scheduled.values()
                if st.start <= start < st.end
            )
            usage = baseline
            seg_start = start
            feasible = True
            for p in points:
                if usage + need > cap:  # 区间 [seg_start, p)
                    feasible = False
                    first_bad = seg_start if first_bad is None else min(first_bad, seg_start)
                    break
                usage += ev[p]
                seg_start = p
            if feasible and usage + need > cap:  # 末段 [seg_start, finish)
                first_bad = seg_start if first_bad is None else min(first_bad, seg_start)
        return first_bad
