"""发布编排库自测（仅标准库 unittest）。运行: python3 -m unittest discover -s tests -v"""

import json
import os
import tempfile
import unittest

from orchestrator import (
    CycleError,
    Executor,
    ResourceOverflowError,
    Step,
    UnknownDependencyError,
    UnknownResourceError,
    World,
    build_plan,
    build_report,
    effect_key,
)


def make_steps(**kwargs_by_name):
    return {name: Step(name, **kw) for name, kw in kwargs_by_name.items()}


class TempStateMixin:
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.state_path = os.path.join(self.tmpdir.name, "state.json")


class TestPlanning(unittest.TestCase):
    def test_topo_order_respects_dependencies(self):
        steps = make_steps(
            db=dict(),
            svc_a=dict(deps=("db",)),
            svc_b=dict(deps=("db",)),
            web=dict(deps=("svc_a", "svc_b")),
        )
        plan = build_plan(steps, {"cpu": 8})
        pos = {n: i for i, n in enumerate(plan.order)}
        for name, step in steps.items():
            for dep in step.deps:
                self.assertLess(pos[dep], pos[name])

    def test_rollback_order_is_reverse_of_execution_order(self):
        steps = make_steps(
            db=dict(),
            svc_a=dict(deps=("db",)),
            svc_b=dict(deps=("db",)),
            web=dict(deps=("svc_a", "svc_b")),
        )
        plan = build_plan(steps, {"cpu": 8})
        self.assertEqual(plan.rollback_order, list(reversed(plan.order)))
        pos = {n: i for i, n in enumerate(plan.rollback_order)}
        for name, step in steps.items():
            for dep in step.deps:
                self.assertGreater(pos[dep], pos[name])

    def test_resource_limit_forces_separate_waves(self):
        steps = make_steps(
            a=dict(resources={"cpu": 3}),
            b=dict(resources={"cpu": 3}),
            c=dict(resources={"cpu": 2}),
        )
        plan = build_plan(steps, {"cpu": 5})
        for wave in plan.waves:
            used = sum(steps[n].resources["cpu"] for n in wave.steps)
            self.assertLessEqual(used, 5)
        self.assertEqual([w.steps for w in plan.waves], [["a", "c"], ["b"]])

    def test_wave_duration_is_max_of_members(self):
        steps = make_steps(
            a=dict(duration=2.0),
            b=dict(duration=5.0),
        )
        plan = build_plan(steps, {"cpu": 8})
        self.assertEqual(plan.makespan, 5.0)

    def test_cycle_reports_full_path(self):
        steps = make_steps(
            a=dict(deps=("c",)),
            b=dict(deps=("a",)),
            c=dict(deps=("b",)),
        )
        with self.assertRaises(CycleError) as ctx:
            build_plan(steps, {"cpu": 8})
        path = ctx.exception.path
        self.assertEqual(path[0], path[-1])
        self.assertEqual(len(path), 4)
        self.assertEqual(set(path), {"a", "b", "c"})
        for u, v in zip(path, path[1:]):
            self.assertIn(v, steps[u].deps)

    def test_self_loop_is_cycle(self):
        steps = make_steps(a=dict(deps=("a",)))
        with self.assertRaises(CycleError) as ctx:
            build_plan(steps, {"cpu": 8})
        self.assertEqual(ctx.exception.path, ["a", "a"])

    def test_unknown_dependency(self):
        steps = make_steps(a=dict(deps=("ghost",)))
        with self.assertRaises(UnknownDependencyError):
            build_plan(steps, {"cpu": 8})

    def test_unknown_resource(self):
        steps = make_steps(a=dict(resources={"gpu": 1}))
        with self.assertRaises(UnknownResourceError):
            build_plan(steps, {"cpu": 8})

    def test_resource_exceeds_capacity(self):
        steps = make_steps(a=dict(resources={"cpu": 16}))
        with self.assertRaises(ResourceOverflowError):
            build_plan(steps, {"cpu": 8})

    def test_empty_plan(self):
        plan = build_plan({}, {"cpu": 8})
        self.assertEqual(plan.order, [])
        self.assertEqual(plan.waves, [])
        self.assertEqual(plan.makespan, 0.0)


class TestExecution(TempStateMixin, unittest.TestCase):
    def scenario(self):
        steps = make_steps(
            db=dict(duration=2.0, resources={"cpu": 2}),
            svc_a=dict(deps=("db",), duration=3.0, resources={"cpu": 3},
                       fail_times=2, max_retries=3),
            svc_b=dict(deps=("db",), duration=1.0, resources={"cpu": 3}),
            web=dict(deps=("svc_a", "svc_b"), duration=1.0, resources={"cpu": 2}),
        )
        plan = build_plan(steps, {"cpu": 8})
        return steps, plan

    def test_successful_run(self):
        steps, plan = self.scenario()
        world = World()
        result = Executor(plan, steps, world, self.state_path).run()
        self.assertEqual(result.status, "success")
        self.assertEqual(result.executed, ["db", "svc_a", "svc_b", "web"])
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.rolled_back, [])
        self.assertEqual(set(world.applied), {effect_key(n) for n in steps})

    def test_retry_is_idempotent(self):
        steps, plan = self.scenario()
        world = World()
        result = Executor(plan, steps, world, self.state_path).run()
        self.assertEqual(result.status, "success")
        self.assertEqual(result.records["svc_a"].attempts, 3)
        applies = [e for e in world.log if e == ("apply", effect_key("svc_a"))]
        skips = [e for e in world.log if e == ("apply-skip", effect_key("svc_a"))]
        # 幂等断言：副作用只真正生效一次，重试的重复写入被去重
        self.assertEqual(len(applies), 1)
        self.assertEqual(len(skips), 2)

    def test_resume_matches_uninterrupted_run(self):
        steps, plan = self.scenario()

        world_full = World()
        path_full = os.path.join(self.tmpdir.name, "full.json")
        result_full = Executor(plan, steps, world_full, path_full).run()
        self.assertEqual(result_full.status, "success")

        world_resumed = World()
        path_paused = os.path.join(self.tmpdir.name, "paused.json")
        first = Executor(plan, steps, world_resumed, path_paused).run(pause_after=2)
        self.assertEqual(first.status, "paused")
        self.assertEqual(first.executed, ["db", "svc_a"])
        with open(path_paused, encoding="utf-8") as f:
            checkpoint = json.load(f)
        self.assertEqual(checkpoint["completed"], ["db", "svc_a"])

        second = Executor(plan, steps, world_resumed, path_paused).run()
        self.assertEqual(second.status, "success")

        # 等价断言：执行顺序、最终副作用、调用序列、逐步记录全部一致
        self.assertEqual(second.executed, result_full.executed)
        self.assertEqual(world_resumed.applied, world_full.applied)
        self.assertEqual(world_resumed.log, world_full.log)
        self.assertEqual(second.records, result_full.records)
        self.assertEqual(second.clock, result_full.clock)

    def test_resume_at_every_boundary(self):
        """在每一步之后都暂停一次，结果仍须与一次跑完一致。"""
        steps, plan = self.scenario()
        world_full = World()
        path_full = os.path.join(self.tmpdir.name, "full.json")
        result_full = Executor(plan, steps, world_full, path_full).run()

        for pause_at in range(1, len(steps)):
            world = World()
            path = os.path.join(self.tmpdir.name, f"p{pause_at}.json")
            Executor(plan, steps, world, path).run(pause_after=pause_at)
            result = Executor(plan, steps, world, path).run()
            self.assertEqual(result.status, "success")
            self.assertEqual(result.executed, result_full.executed)
            self.assertEqual(world.log, world_full.log)
            self.assertEqual(result.records, result_full.records)

    def test_retry_exhaustion_triggers_reverse_rollback(self):
        steps = make_steps(
            db=dict(),
            svc_a=dict(deps=("db",)),
            svc_b=dict(deps=("db",)),
            web=dict(deps=("svc_a", "svc_b"),
                     always_fail=True, max_retries=2),
            monitor=dict(deps=("web",)),
        )
        plan = build_plan(steps, {"cpu": 8})
        world = World()
        result = Executor(plan, steps, world, self.state_path).run()

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.records["web"].attempts, 3)
        # 回滚断言：严格等于已执行步骤的逆序
        # 回滚断言：失败步骤的半成品副作用先补偿，之后严格按已执行步骤逆序
        self.assertEqual(result.rolled_back[0], "web")
        self.assertEqual(result.rolled_back[1:], list(reversed(result.executed)))
        self.assertEqual(result.rolled_back, ["web", "svc_b", "svc_a", "db"])
        # 只有失败步骤的后继被跳过
        self.assertEqual(result.skipped, ["monitor"])
        # 所有副作用（含失败步骤的半成品）都被补偿清除
        self.assertEqual(world.applied, {})
        reverts = [k for kind, k in world.log if kind == "revert"]
        self.assertEqual(reverts, [effect_key(n) for n in ("web", "svc_b", "svc_a", "db")])

    def test_non_retryable_step_fails_immediately(self):
        steps = make_steps(
            a=dict(),
            b=dict(deps=("a",), always_fail=True, retryable=False),
        )
        plan = build_plan(steps, {"cpu": 8})
        result = Executor(plan, steps, World(), self.state_path).run()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.records["b"].attempts, 1)
        self.assertEqual(result.rolled_back, ["b", "a"])

    def test_failed_state_cannot_resume(self):
        steps = make_steps(a=dict(always_fail=True, max_retries=0))
        plan = build_plan(steps, {"cpu": 8})
        Executor(plan, steps, World(), self.state_path).run()
        with self.assertRaises(RuntimeError):
            Executor(plan, steps, World(), self.state_path)

    def test_empty_plan_runs_successfully(self):
        plan = build_plan({}, {"cpu": 8})
        result = Executor(plan, {}, World(), self.state_path).run()
        self.assertEqual(result.status, "success")
        self.assertEqual(result.executed, [])


class TestDiffReport(TempStateMixin, unittest.TestCase):
    def test_report_flags_slow_skipped_and_rolled_back(self):
        steps = make_steps(
            db=dict(duration=1.0, actual_duration=3.0),
            svc=dict(deps=("db",), duration=1.0),
            web=dict(deps=("svc",), always_fail=True, max_retries=0),
            monitor=dict(deps=("web",)),
        )
        plan = build_plan(steps, {"cpu": 8})
        result = Executor(plan, steps, World(), self.state_path).run()
        report = build_report(plan, result)

        self.assertEqual([r.name for r in report.slow_steps], ["db"])
        self.assertEqual(report.skipped_steps, ["monitor"])
        self.assertEqual(report.rolled_back_steps, ["web", "svc", "db"])
        self.assertEqual(report.failed_step, "web")

        by_name = {r.name: r for r in report.rows}
        self.assertEqual(by_name["db"].status, "rolled_back")
        self.assertEqual(by_name["monitor"].status, "skipped")
        self.assertEqual(by_name["web"].status, "failed")

    def test_clean_run_has_no_anomalies(self):
        steps = make_steps(a=dict(), b=dict(deps=("a",)))
        plan = build_plan(steps, {"cpu": 8})
        result = Executor(plan, steps, World(), self.state_path).run()
        report = build_report(plan, result)
        self.assertEqual(report.slow_steps, [])
        self.assertEqual(report.skipped_steps, [])
        self.assertEqual(report.rolled_back_steps, [])
        self.assertIsNone(report.failed_step)


if __name__ == "__main__":
    unittest.main()
