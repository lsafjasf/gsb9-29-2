"""资源受限项目调度（RCPSP）库 —— 仅依赖 Python 标准库。

结合关键路径法（CPM）与资源可行性：
  1. 拓扑排序检测环依赖（含环直接拒绝）；
  2. CPM 前推/回推计算每个任务的最迟开始时间（关键性）；
  3. 串行调度生成方案（SSGS）：按“最迟开始时间”优先级，
     将每个任务安排在满足依赖与资源上限的最早可行时刻。

提供：
  - schedule()      排期，返回每个任务的开始/结束时间与资源占用；
  - validate()      断言 + 事件仿真，验证依赖顺序与任意时刻资源上限；
  - lower_bounds()  关键路径下界与资源下界，用于衡量排期质量。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


class SchedulingError(Exception):
    """调度输入非法（未知前驱、非法工期/资源需求等）。"""


class CyclicDependencyError(SchedulingError):
    """依赖图中存在环，无法调度。"""


@dataclass(frozen=True)
class Task:
    """一个项目任务。

    id:       唯一标识
    duration: 工期（正整数，时间单位）
    preds:    前驱任务 id 列表（必须先完成）
    demand:   执行期间占用的资源单位数（0 <= demand <= capacity）
    """

    id: str
    duration: int
    preds: tuple[str, ...] = ()
    demand: int = 1


@dataclass
class ScheduledTask:
    """排期结果：任务的开始/结束时间与资源占用。"""

    task: Task
    start: int
    end: int  # 结束 = 开始 + 工期（左闭右开区间）

    @property
    def demand(self) -> int:
        return self.task.demand


@dataclass
class ScheduleResult:
    """完整排期结果。"""

    entries: list[ScheduledTask]
    capacity: int
    makespan: int = field(init=False)

    def __post_init__(self) -> None:
        self.makespan = max((e.end for e in self.entries), default=0)

    def by_id(self) -> dict[str, ScheduledTask]:
        return {e.task.id: e for e in self.entries}


# ---------------------------------------------------------------- 输入校验与拓扑


def _check_inputs(tasks: list[Task], capacity: int) -> dict[str, Task]:
    if capacity < 1:
        raise SchedulingError(f"资源上限必须为正整数，得到 {capacity}")
    by_id: dict[str, Task] = {}
    for t in tasks:
        if t.id in by_id:
            raise SchedulingError(f"任务 id 重复: {t.id!r}")
        if t.duration < 1:
            raise SchedulingError(f"任务 {t.id!r} 工期必须为正整数，得到 {t.duration}")
        if not 0 <= t.demand <= capacity:
            raise SchedulingError(
                f"任务 {t.id!r} 资源需求 {t.demand} 超出上限 {capacity}"
            )
        by_id[t.id] = t
    for t in tasks:
        for p in t.preds:
            if p not in by_id:
                raise SchedulingError(f"任务 {t.id!r} 依赖未知任务 {p!r}")
    return by_id


def _topo_order(by_id: dict[str, Task]) -> list[str]:
    """Kahn 拓扑排序；存在环时抛出 CyclicDependencyError。"""
    indeg = {tid: 0 for tid in by_id}
    succs: dict[str, list[str]] = {tid: [] for tid in by_id}
    for t in by_id.values():
        for p in t.preds:
            succs[p].append(t.id)
            indeg[t.id] += 1
    queue = sorted(tid for tid, d in indeg.items() if d == 0)
    order: list[str] = []
    while queue:
        tid = queue.pop(0)
        order.append(tid)
        for s in succs[tid]:
            indeg[s] -= 1
            if indeg[s] == 0:
                queue.append(s)
    if len(order) != len(by_id):
        remaining = sorted(tid for tid, d in indeg.items() if d > 0)
        raise CyclicDependencyError(f"依赖图中存在环，涉及任务: {remaining}")
    return order


# ---------------------------------------------------------------- 关键路径（CPM）


def critical_path_analysis(
    by_id: dict[str, Task], order: list[str]
) -> tuple[dict[str, int], dict[str, int], int]:
    """前推/回推计算最迟开始时间。返回 (earliest_start, latest_start, cp_length)。"""
    es: dict[str, int] = {}
    ef: dict[str, int] = {}
    for tid in order:
        t = by_id[tid]
        es[tid] = max((ef[p] for p in t.preds), default=0)
        ef[tid] = es[tid] + t.duration
    cp_length = max(ef.values(), default=0)

    succs: dict[str, list[str]] = {tid: [] for tid in by_id}
    for t in by_id.values():
        for p in t.preds:
            succs[p].append(t.id)
    lf: dict[str, int] = {}
    ls: dict[str, int] = {}
    for tid in reversed(order):
        t = by_id[tid]
        lf[tid] = min((ls[s] for s in succs[tid]), default=cp_length)
        ls[tid] = lf[tid] - t.duration
    return es, ls, cp_length


# ---------------------------------------------------------------- 排期（SSGS）


def schedule(tasks: list[Task], capacity: int) -> ScheduleResult:
    """结合关键路径优先级与资源可用性的串行调度。

    优先级规则：最迟开始时间（LS）小者优先（越关键越先排），
    平局时工期长、资源需求大者优先。每个任务被安排在满足
    全部前驱完成且资源不超上限的最早时刻。
    """
    by_id = _check_inputs(tasks, capacity)
    order = _topo_order(by_id)
    _, ls, _ = critical_path_analysis(by_id, order)

    priority = sorted(
        by_id.values(), key=lambda t: (ls[t.id], -t.duration, -t.demand, t.id)
    )

    usage: dict[int, int] = {}  # 时刻 -> 已占用资源（整数时间轴）
    finish: dict[str, int] = {}
    entries: list[ScheduledTask] = []

    for t in priority:
        est = max((finish[p] for p in t.preds), default=0)
        start = est
        if t.demand > 0:
            while any(
                usage.get(tau, 0) + t.demand > capacity
                for tau in range(start, start + t.duration)
            ):
                start += 1
        for tau in range(start, start + t.duration):
            usage[tau] = usage.get(tau, 0) + t.demand
        finish[t.id] = start + t.duration
        entries.append(ScheduledTask(task=t, start=start, end=start + t.duration))

    result = ScheduleResult(entries=entries, capacity=capacity)
    validate(result)
    return result


# ---------------------------------------------------------------- 约束验证（断言 + 仿真）


def validate(result: ScheduleResult) -> None:
    """验证排期结果：依赖顺序与任意时刻资源上限均不得违反。

    采用事件仿真：把所有任务的“开始（+demand）/结束（-demand）”
    作为事件按时间回放，逐段断言资源占用不超过上限。
    """
    by_id = result.by_id()
    assert len(by_id) == len(result.entries), "任务 id 必须唯一"

    # 1) 依赖顺序：每个前驱的结束时间 <= 后继的开始时间
    for e in result.entries:
        assert e.end == e.start + e.task.duration, (
            f"任务 {e.task.id!r} 结束时间与工期不一致"
        )
        assert e.start >= 0, f"任务 {e.task.id!r} 开始时间为负"
        for p in e.task.preds:
            pred = by_id[p]
            assert pred.end <= e.start, (
                f"依赖违反: {p!r} 结束于 {pred.end}，"
                f"但 {e.task.id!r} 开始于 {e.start}"
            )

    # 2) 资源上限：事件仿真，任意时刻占用 <= capacity
    events: list[tuple[int, int]] = []  # (时刻, 资源变化量)
    for e in result.entries:
        if e.demand:
            events.append((e.start, +e.demand))
            events.append((e.end, -e.demand))
    # 同一时刻先处理释放（结束）再处理占用（开始），与左闭右开区间一致
    events.sort(key=lambda ev: (ev[0], ev[1]))
    load = 0
    for time, delta in events:
        load += delta
        assert 0 <= load <= result.capacity, (
            f"资源超限: 时刻 {time} 占用 {load} > 上限 {result.capacity}"
        )
    assert load == 0, "仿真结束后资源应全部释放"


# ---------------------------------------------------------------- 下界对比


def lower_bounds(tasks: list[Task], capacity: int) -> dict[str, float]:
    """计算排期质量下界。

    lb_critical_path: 关键路径长度（忽略资源的最短可能工期）
    lb_resource:      资源下界 ceil(总工作量 / 资源上限)
    lb:               两者较大者（任何可行调度的 makespan 下界）
    """
    by_id = _check_inputs(tasks, capacity)
    order = _topo_order(by_id)
    _, _, cp = critical_path_analysis(by_id, order)
    work = sum(t.duration * t.demand for t in tasks)
    lb_res = math.ceil(work / capacity)
    return {
        "lb_critical_path": cp,
        "lb_resource": lb_res,
        "lb": max(cp, lb_res),
        "total_work": work,
    }


def compare_with_bounds(tasks: list[Task], capacity: int) -> dict[str, float]:
    """排期并与下界对比，返回 makespan 及各比值。"""
    result = schedule(tasks, capacity)
    bounds = lower_bounds(tasks, capacity)
    lb = bounds["lb"]
    ratio = result.makespan / lb if lb else 1.0
    return {
        "makespan": result.makespan,
        **bounds,
        "ratio_to_lb": ratio,
        "ratio_to_critical_path": (
            result.makespan / bounds["lb_critical_path"]
            if bounds["lb_critical_path"]
            else 1.0
        ),
        "ratio_to_resource_lb": (
            result.makespan / bounds["lb_resource"] if bounds["lb_resource"] else 1.0
        ),
    }


# ---------------------------------------------------------------- 演示


def _demo() -> None:
    # 示例项目：6 个任务，3 单位人力
    tasks = [
        Task("A 设计", duration=3, demand=2),
        Task("B 采购", duration=2, demand=1),
        Task("C 开发", duration=4, preds=("A 设计",), demand=2),
        Task("D 联调", duration=2, preds=("B 采购", "C 开发"), demand=2),
        Task("E 文档", duration=2, preds=("A 设计",), demand=1),
        Task("F 发布", duration=1, preds=("D 联调", "E 文档"), demand=1),
    ]
    capacity = 3
    result = schedule(tasks, capacity)
    stats = compare_with_bounds(tasks, capacity)

    print(f"资源上限: {capacity}  任务数: {len(tasks)}")
    print(f"{'任务':<10}{'开始':>4}{'结束':>4}{'占用':>6}  前驱")
    for e in sorted(result.entries, key=lambda x: (x.start, x.task.id)):
        preds = ",".join(e.task.preds) or "-"
        print(f"{e.task.id:<10}{e.start:>4}{e.end:>4}{e.demand:>6}  {preds}")

    print(f"\n总工期 makespan        = {stats['makespan']}")
    print(f"关键路径下界 LB_cp     = {stats['lb_critical_path']}")
    print(f"资源下界 LB_res        = {stats['lb_resource']}")
    print(f"综合下界 LB            = {stats['lb']}")
    print(f"makespan / LB          = {stats['ratio_to_lb']:.3f}")
    print(f"makespan / LB_cp       = {stats['ratio_to_critical_path']:.3f}")
    print(f"makespan / LB_res      = {stats['ratio_to_resource_lb']:.3f}")
    print(
        "\n差距来源：makespan/LB > 1 说明资源冲突迫使部分非关键任务让位，\n"
        "关键路径上的等待与资源整形（resource shaping）造成超出下界的空闲。"
    )


if __name__ == "__main__":
    _demo()
