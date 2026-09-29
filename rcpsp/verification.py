"""独立的约束仿真验证（不依赖调度器内部实现，只读结果做重放）。

逐整数时间步扫描 [0, makespan)，断言：
1. 时间合法：start>=0、end=start+duration、区间不异常。
2. 依赖顺序：每个任务开始时，其所有前驱均已结束。
3. 资源上限：每个时刻、每种资源的并发占用绝不超过容量。
"""

from __future__ import annotations

from .scheduler import ScheduleResult, SchedulingError


def assert_schedule_valid(result: ScheduleResult) -> None:
    tasks = result.tasks

    assert tasks, "调度结果为空"
    for st in tasks.values():
        assert st.start >= 0, f"任务 {st.tid} 开始时间为负: {st.start}"
        assert st.end == st.start + st.duration, f"任务 {st.tid} 起止与工期不一致"
        assert st.duration > 0, f"任务 {st.tid} 工期非正"
        assert st.end <= result.makespan, f"任务 {st.tid} 超出 makespan"
        for r, d in st.demand.items():
            assert d >= 0
            assert d <= result.capacities.get(r, 0), (
                f"任务 {st.tid} 单任务需求 {r}={d} 超容量"
            )

    # 1) 依赖顺序（逐任务断言）
    for st in tasks.values():
        for p in st.predecessors:
            assert p in tasks, f"任务 {st.tid} 的前驱 {p} 不存在"
            assert tasks[p].end <= st.start, (
                f"依赖违反: {p} 在 t={tasks[p].end} 结束，"
                f"但 {st.tid} 在 t={st.start} 开始"
            )

    # 2) 逐时间步资源仿真（左闭右开 [t, t+1)）
    horizon = result.makespan
    for t in range(horizon):
        for resource, cap in result.capacities.items():
            running = [
                st.tid
                for st in tasks.values()
                if st.start <= t < st.end and st.demand.get(resource, 0) > 0
            ]
            usage = sum(tasks[tid].demand[resource] for tid in running)
            assert usage <= cap, (
                f"资源 {resource} 在 t={t} 超限: 占用 {usage} > 容量 {cap}，"
                f"运行任务 {running}"
            )

    # 3) 关键路径下界：不考虑资源时任何可行工期都不可能短于 CP
    lbs = result.lower_bounds
    assert result.makespan >= result.critical_path_length
    assert result.makespan >= int(lbs["overall"]), (
        f"makespan {result.makespan} 低于理论下界 {lbs['overall']}"
    )


def simulate(result: ScheduleResult, verbose: bool = False) -> dict[str, object]:
    """重放调度，返回仿真摘要；任何约束被违反都会抛 AssertionError。"""
    assert_schedule_valid(result)
    peak_usage: dict[str, int] = {}
    for resource in result.capacities:
        peak = 0
        for t in range(result.makespan):
            usage = sum(
                st.demand.get(resource, 0)
                for st in tasks_running_at(result, t)
            )
            peak = max(peak, usage)
        peak_usage[resource] = peak
    summary = {
        "makespan": result.makespan,
        "peak_usage": peak_usage,
        "violations": 0,
        "feasible": True,
    }
    if verbose:
        print(f"仿真通过: makespan={result.makespan}, 峰值占用={peak_usage}")
    return summary


def tasks_running_at(result: ScheduleResult, t: int):
    return [st for st in result.tasks.values() if st.start <= t < st.end]


def compare_with_lower_bounds(result: ScheduleResult) -> dict[str, float]:
    """返回 makespan 与各下界的比值（>=1.0；越大说明差距越大）。"""
    cp = result.critical_path_length
    overall = int(result.lower_bounds["overall"])
    resource_lbs = result.lower_bounds["resource_work_lower_bounds"]
    max_res_lb = max([int(v) for v in resource_lbs.values()] or [0])
    cp_den = max(cp, 1)
    overall_den = max(overall, 1)
    return {
        "makespan": float(result.makespan),
        "critical_path_length": float(cp),
        "resource_lower_bound": float(max_res_lb),
        "overall_lower_bound": float(overall),
        "ratio_vs_critical_path": round(result.makespan / cp_den, 4),
        "ratio_vs_resource_lb": (
            round(result.makespan / max_res_lb, 4) if max_res_lb > 0 else None
        ),
        "ratio_vs_overall_lb": round(result.makespan / overall_den, 4),
    }
