"""演示：构造若干场景并打印排期、资源占用与下界对比。"""

from __future__ import annotations

from .scheduler import Scheduler
from .verification import assert_schedule_valid, compare_with_lower_bounds


def print_scenario(title: str, sched: Scheduler) -> None:
    result = sched.schedule()
    assert_schedule_valid(result)  # 断言：依赖 + 资源全程合法
    cmp_ = compare_with_lower_bounds(result)
    lb = result.lower_bounds

    print("=" * 72)
    print(title)
    print("=" * 72)
    print(result.format_table())
    print(f"\n关键路径: {' -> '.join(result.critical_path)}  长度={result.critical_path_length}")
    print(f"资源工作量下界: {lb['resource_work_lower_bounds']}  "
          f"最长任务={lb['longest_task']}  综合下界={lb['overall']}")
    print(f"实际工期 makespan = {result.makespan}")
    print(f"  比值 vs 关键路径   = {cmp_['ratio_vs_critical_path']}")
    print(f"  比值 vs 资源下界   = {cmp_['ratio_vs_resource_lb']}")
    print(f"  比值 vs 综合下界   = {cmp_['ratio_vs_overall_lb']}")
    for resource, cap in result.capacities.items():
        timeline = result.resource_timeline(resource)
        rendered = " ".join(f"[{a},{b}):{u}/{cap}" for a, b, u in timeline)
        print(f"资源 {resource} 占用曲线: {rendered or '(全程空闲)'}")
    print()


def scenario_single() -> Scheduler:
    s = Scheduler(capacities=1)
    s.add_task("A", duration=5, demand=1)
    return s


def scenario_resource_abundant() -> Scheduler:
    # 容量充足：应完全按关键路径推进，makespan == CP
    s = Scheduler(capacities=10)
    s.add_task("设计", 3, 2)
    s.add_task("前端", 4, 3, predecessors=["设计"])
    s.add_task("后端", 5, 3, predecessors=["设计"])
    s.add_task("联调", 2, 2, predecessors=["前端", "后端"])
    s.add_task("文档", 3, 1, predecessors=["设计"])
    return s


def scenario_extreme_contention() -> Scheduler:
    # 只有 1 个人力：一切可并行的工作被迫串行
    s = Scheduler(capacities=1)
    s.add_task("A", 3, 1)
    s.add_task("B", 4, 1)
    s.add_task("C", 2, 1)
    s.add_task("D", 5, 1, predecessors=["A"])
    return s


def scenario_multi_resource() -> Scheduler:
    # 两种资源：工程师 2 人、测试机 1 台
    s = Scheduler(capacities={"工程师": 2, "测试机": 1})
    s.add_task("模块X开发", 4, {"工程师": 1})
    s.add_task("模块Y开发", 3, {"工程师": 1})
    s.add_task("模块X测试", 2, {"工程师": 1, "测试机": 1}, predecessors=["模块X开发"])
    s.add_task("模块Y测试", 2, {"工程师": 1, "测试机": 1}, predecessors=["模块Y开发"])
    s.add_task("集成", 2, {"工程师": 2}, predecessors=["模块X测试", "模块Y测试"])
    return s


def scenario_fragmentation_gap() -> Scheduler:
    # 三个占满 2 人力、工期 2 的长任务 + 一个占 1 人力、工期 2 的短任务。
    # 工作量=14，下界 ceil(14/2)=7；但长任务区间容量全满，短任务无法
    # 并行，只能排到最后 => makespan=8。差距源于资源下界只算总工作量，
    # 忽略了任务不可拆分与装箱可行性。
    s = Scheduler(capacities=2)
    s.add_task("长任务1", 2, 2)
    s.add_task("长任务2", 2, 2)
    s.add_task("长任务3", 2, 2)
    s.add_task("短任务1", 2, 1)
    return s


def main() -> None:
    print_scenario("场景1：单任务", scenario_single())
    print_scenario("场景2：资源充足（应等于关键路径）", scenario_resource_abundant())
    print_scenario("场景3：资源极度紧张（人力=1，近乎全串行）", scenario_extreme_contention())
    print_scenario("场景4：多资源（工程师 + 测试机）", scenario_multi_resource())
    print_scenario("场景5：碎片/不可拆分导致高于下界", scenario_fragmentation_gap())


if __name__ == "__main__":
    main()
