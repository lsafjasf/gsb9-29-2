"""自测：边界用例 + 断言仿真 + 下界对比 + 随机模糊测试。

运行：python -m rcpsp.test_scheduler    （在项目根目录下）
"""

from __future__ import annotations

import random
import unittest

from rcpsp.scheduler import Scheduler, SchedulingError
from rcpsp.verification import assert_schedule_valid, compare_with_lower_bounds, simulate


class TestSingleTask(unittest.TestCase):
    def test_single_task(self):
        s = Scheduler(capacities=1)
        s.add_task("A", 5, 1)
        r = s.schedule()
        assert_schedule_valid(r)
        self.assertEqual((r.tasks["A"].start, r.tasks["A"].end), (0, 5))
        self.assertEqual(r.makespan, 5)
        cmp_ = compare_with_lower_bounds(r)
        self.assertEqual(cmp_["ratio_vs_critical_path"], 1.0)
        self.assertEqual(cmp_["ratio_vs_overall_lb"], 1.0)
        self.assertEqual(simulate(r)["peak_usage"], {"R": 1})

    def test_single_task_multi_resource(self):
        s = Scheduler(capacities={"人": 3, "机": 2})
        s.add_task("A", 4, {"人": 2, "机": 1})
        r = s.schedule()
        assert_schedule_valid(r)
        self.assertEqual(r.makespan, 4)


class TestResourceAbundant(unittest.TestCase):
    def test_makespan_equals_critical_path(self):
        s = Scheduler(capacities=100)
        s.add_task("a", 3, 2)
        s.add_task("b", 4, 3, predecessors=["a"])
        s.add_task("c", 5, 3, predecessors=["a"])
        s.add_task("d", 2, 2, predecessors=["b", "c"])
        s.add_task("e", 6, 1, predecessors=["a"])
        r = s.schedule()
        assert_schedule_valid(r)
        self.assertEqual(r.critical_path_length, 10)  # a(3)+c(5)+d(2)
        self.assertEqual(r.makespan, 10)
        cmp_ = compare_with_lower_bounds(r)
        self.assertEqual(cmp_["ratio_vs_critical_path"], 1.0)

    def test_independent_parallel_tasks(self):
        s = Scheduler(capacities=3)
        for i in range(3):
            s.add_task(f"t{i}", 4, 1)
        r = s.schedule()
        assert_schedule_valid(r)
        self.assertEqual(r.makespan, 4)  # 全部并行
        self.assertEqual(simulate(r)["peak_usage"]["R"], 3)


class TestExtremeContention(unittest.TestCase):
    def test_single_capacity_serializes_all(self):
        s = Scheduler(capacities=1)
        for i, dur in enumerate([3, 4, 2, 5]):
            s.add_task(f"t{i}", dur, 1)
        r = s.schedule()
        assert_schedule_valid(r)
        # 容量 1 且无依赖：任何时刻只能跑一个，makespan 必为总工作量
        self.assertEqual(r.makespan, 14)
        self.assertEqual(r.lower_bounds["resource_work_lower_bounds"], {"R": 14})
        cmp_ = compare_with_lower_bounds(r)
        self.assertEqual(cmp_["ratio_vs_resource_lb"], 1.0)
        # 关键路径只有最长任务 5，资源受限使其膨胀近 3 倍
        self.assertGreater(cmp_["ratio_vs_critical_path"], 2.5)

    def test_diamond_dag_with_one_worker(self):
        s = Scheduler(capacities=1)
        s.add_task("root", 2, 1)
        s.add_task("l1", 3, 1, predecessors=["root"])
        s.add_task("l2", 2, 1, predecessors=["root"])
        s.add_task("l3", 1, 1, predecessors=["root"])
        s.add_task("done", 1, 1, predecessors=["l1", "l2", "l3"])
        r = s.schedule()
        assert_schedule_valid(r)
        # CP = 2+3+1 = 6，但资源=1 => 串行总工作量 9
        self.assertEqual(r.critical_path_length, 6)
        self.assertEqual(r.makespan, 9)

    def test_zero_demand_task_does_not_consume_resource(self):
        s = Scheduler(capacities=1)
        s.add_task("a", 3, 1)
        s.add_task("空闲等待", 2, 0)  # 纯里程碑式等待，不占人力
        r = s.schedule()
        assert_schedule_valid(r)
        self.assertEqual(r.makespan, 3)


class TestMultipleResources(unittest.TestCase):
    def test_two_resources(self):
        s = Scheduler(capacities={"工程师": 2, "测试机": 1})
        s.add_task("x", 4, {"工程师": 1})
        s.add_task("y", 3, {"工程师": 1})
        s.add_task("xt", 2, {"工程师": 1, "测试机": 1}, predecessors=["x"])
        s.add_task("yt", 2, {"工程师": 1, "测试机": 1}, predecessors=["y"])
        s.add_task("集成", 2, {"工程师": 2}, predecessors=["xt", "yt"])
        r = s.schedule()
        assert_schedule_valid(r)
        self.assertLessEqual(r.makespan, r.lower_bounds["overall"] + 2)
        # 测试机只有 1 台，两个测试任务必须串行
        self.assertNotEqual(r.tasks["xt"].start, r.tasks["yt"].start)


class TestLowerBoundGap(unittest.TestCase):
    def test_fragmentation_makespan_above_work_lb(self):
        # 容量 2：三个各占满 2 人力、工期 2 的长任务 + 一个占 1 人力、
        # 工期 2 的短任务。工作量=3*2*2+2=14，下界 ceil(14/2)=7；
        # 但长任务执行区间容量全满，短任务无法与其并行，最短也只能 8。
        # 差距来自资源下界忽略了不可拆分与装箱可行性。
        s = Scheduler(capacities=2)
        s.add_task("big1", 2, 2)
        s.add_task("big2", 2, 2)
        s.add_task("big3", 2, 2)
        s.add_task("small1", 2, 1)
        r = s.schedule()
        assert_schedule_valid(r)
        self.assertEqual(r.lower_bounds["resource_work_lower_bounds"], {"R": 7})
        self.assertEqual(r.makespan, 8)
        cmp_ = compare_with_lower_bounds(r)
        self.assertGreater(cmp_["ratio_vs_overall_lb"], 1.0)

    def test_ratios_always_ge_one(self):
        s = Scheduler(capacities=2)
        s.add_task("a", 2, 2)
        s.add_task("b", 3, 1, predecessors=["a"])
        s.add_task("c", 2, 1)
        r = s.schedule()
        assert_schedule_valid(r)
        cmp_ = compare_with_lower_bounds(r)
        for key in (
            "ratio_vs_critical_path",
            "ratio_vs_overall_lb",
        ):
            self.assertGreaterEqual(cmp_[key], 1.0)
        if cmp_["ratio_vs_resource_lb"] is not None:
            self.assertGreaterEqual(cmp_["ratio_vs_resource_lb"], 1.0)


class TestCyclesAndInvalidInput(unittest.TestCase):
    def test_self_loop_rejected(self):
        s = Scheduler(capacities=1)
        s.add_task("a", 1, 1, predecessors=["a"])
        with self.assertRaises(SchedulingError):
            s.schedule()

    def test_simple_cycle_rejected(self):
        s = Scheduler(capacities=1)
        s.add_task("a", 1, 1, predecessors=["b"])
        s.add_task("b", 1, 1, predecessors=["a"])
        with self.assertRaises(SchedulingError) as ctx:
            s.schedule()
        self.assertIn("环", str(ctx.exception))

    def test_three_node_cycle_rejected_with_path(self):
        s = Scheduler(capacities=1)
        s.add_task("a", 1, predecessors=["c"])
        s.add_task("b", 1, predecessors=["a"])
        s.add_task("c", 1, predecessors=["b"])
        s.add_task("ok", 1)
        with self.assertRaises(SchedulingError) as ctx:
            s.schedule()
        msg = str(ctx.exception)
        self.assertIn("a", msg)
        self.assertIn("b", msg)
        self.assertIn("c", msg)

    def test_missing_predecessor_rejected(self):
        s = Scheduler(capacities=1)
        s.add_task("a", 1, predecessors=["ghost"])
        with self.assertRaises(SchedulingError):
            s.schedule()

    def test_duplicate_task_rejected(self):
        s = Scheduler(capacities=1)
        s.add_task("a", 1)
        with self.assertRaises(SchedulingError):
            s.add_task("a", 1)

    def test_bad_duration_rejected(self):
        s = Scheduler(capacities=1)
        with self.assertRaises(SchedulingError):
            s.add_task("a", 0)

    def test_demand_exceeding_capacity_rejected(self):
        s = Scheduler(capacities=2)
        with self.assertRaises(SchedulingError):
            s.add_task("a", 1, demand=3)

    def test_unknown_resource_is_not_auto_created(self):
        s = Scheduler(capacities={"人": 1})
        with self.assertRaises(SchedulingError):
            s.add_task("a", 1, demand={"机器": 1})


class TestResourceOccupancyTimeline(unittest.TestCase):
    def test_timeline_matches_simulation(self):
        s = Scheduler(capacities=2)
        s.add_task("a", 3, 2)
        s.add_task("b", 2, 1)
        r = s.schedule()
        assert_schedule_valid(r)
        # 任意分段占用都不得超过容量
        for a, b, u in r.resource_timeline("R"):
            self.assertLessEqual(u, 2)
            self.assertLess(a, b)


class TestFuzz(unittest.TestCase):
    """随机 DAG + 随机资源：每一例都做全量约束仿真与下界不变量检查。"""

    def test_random_dags(self):
        rng = random.Random(20260930)
        for case in range(300):
            n = rng.randint(1, 12)
            resources = ["R"] if case % 3 == 0 else ["工程师", "测试机"]
            caps = {
                r: rng.randint(1, 3) if r == "工程师" else rng.randint(1, 2)
                for r in resources
            }
            if "R" in resources:
                caps = {"R": rng.randint(1, 3)}
            s = Scheduler(capacities=caps)
            for i in range(n):
                # 只允许指向更早的任务 => 必然无环
                preds = [
                    f"t{j}"
                    for j in range(i)
                    if rng.random() < 0.25
                ]
                demand = {r: rng.randint(0, caps[r]) for r in caps}
                s.add_task(
                    f"t{i}",
                    duration=rng.randint(1, 6),
                    demand=demand if len(caps) > 1 else demand["R"],
                    predecessors=preds,
                )
            r = s.schedule()
            assert_schedule_valid(r)
            simulate(r)
            cmp_ = compare_with_lower_bounds(r)
            self.assertGreaterEqual(r.makespan, r.critical_path_length)
            self.assertGreaterEqual(
                r.makespan, int(r.lower_bounds["overall"])
            )
            for key in (
                "ratio_vs_critical_path",
                "ratio_vs_overall_lb",
            ):
                self.assertGreaterEqual(cmp_[key], 1.0)
            if cmp_["ratio_vs_resource_lb"] is not None:
                self.assertGreaterEqual(cmp_["ratio_vs_resource_lb"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
