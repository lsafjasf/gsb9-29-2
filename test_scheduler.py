"""scheduler 的自测：覆盖单任务、资源充足、资源极度紧张、含环依赖，
并对每个场景断言依赖顺序与任意时刻资源上限，同时对比下界。"""

import unittest

from scheduler import (
    CyclicDependencyError,
    ScheduleResult,
    ScheduledTask,
    SchedulingError,
    Task,
    compare_with_bounds,
    lower_bounds,
    schedule,
    validate,
)


class TestScheduleConstraints(unittest.TestCase):
    """通用约束验证：任何排期结果都必须通过 validate 的断言与仿真。"""

    def assert_valid(self, result: ScheduleResult) -> None:
        validate(result)  # 内部含依赖顺序断言与资源事件仿真

    def test_single_task(self):
        tasks = [Task("only", duration=5, demand=2)]
        result = schedule(tasks, capacity=2)
        self.assert_valid(result)
        entry = result.entries[0]
        self.assertEqual((entry.start, entry.end), (0, 5))
        self.assertEqual(result.makespan, 5)
        stats = compare_with_bounds(tasks, capacity=2)
        self.assertEqual(stats["ratio_to_lb"], 1.0)

    def test_abundant_resources_matches_critical_path(self):
        # 资源充足时，工期应等于关键路径长度（下界可达）
        tasks = [
            Task("a", duration=3, demand=1),
            Task("b", duration=2, demand=1),
            Task("c", duration=4, preds=("a",), demand=1),
            Task("d", duration=1, preds=("b", "c"), demand=1),
        ]
        capacity = 4
        result = schedule(tasks, capacity)
        self.assert_valid(result)
        stats = compare_with_bounds(tasks, capacity)
        self.assertEqual(result.makespan, stats["lb_critical_path"])  # 3+4+1=8
        self.assertEqual(stats["ratio_to_lb"], 1.0)
        # 资源充足时所有任务都应按最早开始时间启动
        starts = {e.task.id: e.start for e in result.entries}
        self.assertEqual(starts, {"a": 0, "b": 0, "c": 3, "d": 7})

    def test_extremely_scarce_resources_serializes(self):
        # 容量 1、每任务需 1：所有任务被迫串行，工期 = 总工期和 = 资源下界
        tasks = [
            Task("t1", duration=2, demand=1),
            Task("t2", duration=3, demand=1),
            Task("t3", duration=1, preds=("t1",), demand=1),
            Task("t4", duration=4, demand=1),
        ]
        result = schedule(tasks, capacity=1)
        self.assert_valid(result)
        total = sum(t.duration for t in tasks)
        self.assertEqual(result.makespan, total)
        stats = compare_with_bounds(tasks, capacity=1)
        self.assertEqual(stats["lb_resource"], total)
        self.assertEqual(stats["ratio_to_lb"], 1.0)
        # 任意两任务区间不得重叠（串行）
        intervals = sorted((e.start, e.end) for e in result.entries)
        for (_, prev_end), (next_start, _) in zip(intervals, intervals[1:]):
            self.assertLessEqual(prev_end, next_start)

    def test_cyclic_dependency_rejected(self):
        tasks = [
            Task("a", duration=1, preds=("c",)),
            Task("b", duration=1, preds=("a",)),
            Task("c", duration=1, preds=("b",)),
        ]
        with self.assertRaises(CyclicDependencyError):
            schedule(tasks, capacity=2)
        with self.assertRaises(CyclicDependencyError):
            lower_bounds(tasks, capacity=2)

    def test_self_loop_rejected(self):
        with self.assertRaises(CyclicDependencyError):
            schedule([Task("x", duration=1, preds=("x",))], capacity=1)

    def test_invalid_inputs_rejected(self):
        with self.assertRaises(SchedulingError):  # 需求超过上限
            schedule([Task("a", duration=1, demand=3)], capacity=2)
        with self.assertRaises(SchedulingError):  # 未知前驱
            schedule([Task("a", duration=1, preds=("ghost",))], capacity=2)
        with self.assertRaises(SchedulingError):  # 非正工期
            schedule([Task("a", duration=0)], capacity=2)
        with self.assertRaises(SchedulingError):  # 重复 id
            schedule([Task("a", duration=1), Task("a", duration=1)], capacity=2)
        with self.assertRaises(SchedulingError):  # 非法容量
            schedule([Task("a", duration=1)], capacity=0)

    def test_resource_conflict_gap_over_bound(self):
        # 资源碎片化导致 makespan 超过下界：容量 3，每个任务占 2，
        # 任意两任务无法并行（2+2>3），只能串行，最优工期 6；
        # 而 LB = max(关键路径 2, 总工作量 12/容量 3 = 4) = 4，比值 1.5。
        # 差距来源：每个任务占用 2/3 容量，剩余 1/3 被闲置（资源整形损失）。
        tasks = [
            Task("a", duration=2, demand=2),
            Task("b", duration=2, demand=2),
            Task("c", duration=2, demand=2),
        ]
        capacity = 3
        result = schedule(tasks, capacity)
        self.assert_valid(result)
        stats = compare_with_bounds(tasks, capacity)
        self.assertEqual(stats["lb"], 4)
        self.assertEqual(result.makespan, 6)
        self.assertGreater(stats["ratio_to_lb"], 1.0)
        self.assertAlmostEqual(stats["ratio_to_lb"], 1.5)

    def test_validate_detects_dependency_violation(self):
        # 手工构造一个违反依赖的结果，validate 必须断言失败
        bad = ScheduleResult(
            entries=[
                ScheduledTask(Task("a", duration=2), start=0, end=2),
                ScheduledTask(Task("b", duration=1, preds=("a",)), start=1, end=2),
            ],
            capacity=2,
        )
        with self.assertRaises(AssertionError):
            validate(bad)

    def test_validate_detects_capacity_violation(self):
        bad = ScheduleResult(
            entries=[
                ScheduledTask(Task("a", duration=2, demand=2), start=0, end=2),
                ScheduledTask(Task("b", duration=2, demand=2), start=0, end=2),
            ],
            capacity=3,
        )
        with self.assertRaises(AssertionError):
            validate(bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
