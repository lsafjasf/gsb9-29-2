#!/usr/bin/env python3
"""编排库自测：规划、资源、循环依赖、续跑、幂等、回滚、差异报告、边界。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from orchestrator import (CyclicDependencyError, Executor, HaltRun,
                          PlanError, Planner, RetryPolicy, Resources, Step)


def noop(_ctx):
    return None


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="orch-test-")
        self.state = os.path.join(self.dir, "checkpoint.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def executor(self, steps, limits, **kw):
        kw.setdefault("sleeper", lambda _s: None)
        return Executor(steps, limits, self.state, **kw)


class TestPlanning(Base):
    def test_topological_order_and_waves(self):
        steps = [
            Step("a", noop, resources={"cpu": 1}),
            Step("b", noop, requires=["a"], resources={"cpu": 2}),
            Step("c", noop, requires=["a"], resources={"cpu": 2}),
            Step("d", noop, requires=["b", "c"], resources={"cpu": 1}),
        ]
        plan = Planner(steps, {"cpu": 3}).make_plan()
        self.assertEqual(plan.order, ["a", "b", "c", "d"])
        pos = {sid: i for i, sid in enumerate(plan.order)}
        self.assertLess(pos["a"], pos["b"])
        self.assertLess(pos["a"], pos["c"])
        self.assertLess(pos["b"], pos["d"])
        self.assertLess(pos["c"], pos["d"])

    def test_resource_cap_serializes_parallel_branches(self):
        # b、c 各需 cpu=2，上限 3 => 计划中不能同 wave
        steps = [
            Step("a", noop, resources={"cpu": 1}),
            Step("b", noop, requires=["a"], resources={"cpu": 2},
                 duration_estimate=2.0),
            Step("c", noop, requires=["a"], resources={"cpu": 2},
                 duration_estimate=2.0),
        ]
        plan = Planner(steps, {"cpu": 3}).make_plan()
        waves = plan.waves
        b_wave = next(i for i, w in enumerate(waves) if "b" in w)
        c_wave = next(i for i, w in enumerate(waves) if "c" in w)
        self.assertNotEqual(b_wave, c_wave)
        self.assertEqual(plan.estimated_makespan, 5.0)  # 1(a)+2(b)+2(c)

    def test_cycle_reports_full_path(self):
        steps = [
            Step("a", noop, requires=["c"]),
            Step("b", noop, requires=["a"]),
            Step("c", noop, requires=["b"]),
            Step("z", noop),
        ]
        with self.assertRaises(CyclicDependencyError) as cm:
            Planner(steps, {"cpu": 4})
        cycle = cm.exception.cycle
        self.assertEqual(cycle[0], cycle[-1])
        self.assertEqual(len(cycle), 4)  # 环上 3 个节点 + 收尾起点
        self.assertEqual(set(cycle[:-1]), {"a", "b", "c"})
        self.assertEqual(cycle[0], cycle[-1])
        # DFS 方向确定：a 依赖 c，故路径为 a -> c -> b -> a
        self.assertEqual(cycle, ["a", "c", "b", "a"])

    def test_self_loop_is_cycle(self):
        with self.assertRaises(CyclicDependencyError) as cm:
            Planner([Step("a", noop, requires=["a"])], {"cpu": 1})
        self.assertEqual(cm.exception.cycle, ["a", "a"])

    def test_unknown_dependency_rejected(self):
        with self.assertRaises(PlanError):
            Planner([Step("a", noop, requires=["ghost"])], {"cpu": 1})

    def test_step_exceeding_resource_limit_rejected(self):
        with self.assertRaises(PlanError):
            Planner([Step("a", noop, resources={"cpu": 5})], {"cpu": 3})

    def test_duplicate_step_id_rejected(self):
        with self.assertRaises(PlanError):
            Planner([Step("a", noop), Step("a", noop)], {"cpu": 1})

    def test_empty_plan(self):
        plan = Planner([], {"cpu": 1}).make_plan()
        self.assertEqual(plan.order, [])
        result = self.executor([], {"cpu": 1}).run()
        self.assertEqual(result.status, "SUCCESS")


class TestExecution(Base):
    def test_success_run_and_report_fields(self):
        steps = [
            Step("a", lambda ctx: ctx.do_once("k", lambda: "A"),
                 resources={"cpu": 1}),
            Step("b", lambda ctx: ctx.do_once("k", lambda: "B"),
                 requires=["a"], resources={"cpu": 1}),
        ]
        result = self.executor(steps, {"cpu": 2}).run()
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.execution_order, ["a", "b"])
        self.assertEqual(len(result.effect_ledger), 2)
        self.assertEqual(result.rollback, [])

    def test_threaded_resource_gating(self):
        # 两个步骤各需 cpu=2，上限 3 => 并发执行时重叠窗口必须为空
        intervals = []
        lock = threading.Lock()

        def make(name):
            def action(_ctx):
                start = time.monotonic()
                time.sleep(0.08)
                with lock:
                    intervals.append((name, start, time.monotonic()))
            return action

        steps = [
            Step("b", make("b"), resources={"cpu": 2}),
            Step("c", make("c"), resources={"cpu": 2}),
        ]
        result = self.executor(steps, {"cpu": 3}, max_workers=4).run()
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(len(intervals), 2)
        (n1, s1, e1), (n2, s2, e2) = sorted(intervals, key=lambda x: x[1])
        self.assertGreaterEqual(s2, e1 - 1e-3,
                                "资源上限被突破: %s 与 %s 重叠" % (n1, n2))

    def test_retry_idempotent_success(self):
        calls = {"n": 0}

        def flaky(ctx):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("boom %d" % calls["n"])
            return ctx.do_once("effect", lambda: "done")

        steps = [Step("a", flaky, retriable=True,
                      retry_policy=RetryPolicy(max_attempts=3))]
        result = self.executor(steps, {"cpu": 1}).run()
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.steps["a"].attempts, 3)
        self.assertEqual(calls["n"], 3)
        effects = [e for e in result.effect_ledger if e["key"] == "effect"]
        self.assertEqual(len(effects), 1)  # 幂等：副作用只产生一次

    def test_retry_exhausted_then_failure_handling(self):
        calls = {"n": 0}

        def always_fail(_ctx):
            calls["n"] += 1
            raise RuntimeError("still broken")

        steps = [
            Step("ok", noop),
            Step("bad", always_fail, requires=["ok"], retriable=True,
                 retry_policy=RetryPolicy(max_attempts=2),
                 rollback=lambda ctx: ctx.do_once("rb", lambda: "undo-bad")),
            Step("downstream", noop, requires=["bad"]),
        ]
        result = self.executor(steps, {"cpu": 4}).run()
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(calls["n"], 2)  # 达到上限后停止重试
        self.assertEqual(result.steps["bad"].status, "FAILED")
        self.assertEqual(result.steps["bad"].rollback_status, "ROLLED_BACK")
        self.assertEqual(result.steps["downstream"].status, "SKIPPED")
        self.assertIn("downstream", result.skipped)
        # 回滚逆序：bad 先回滚，ok 后回滚（ok 无 rollback => NOOP）
        rb_ids = [r.id for r in result.rollback]
        self.assertEqual(rb_ids, ["bad", "ok"])
        self.assertEqual(result.rollback[0].status, "ROLLED_BACK")
        self.assertEqual(result.rollback[1].status, "NOOP")

    def test_rollback_is_reverse_of_execution(self):
        rolled = []

        def mk(name):
            def action(ctx):
                return ctx.do_once("effect", lambda: name)
            def rb(ctx):
                rolled.append(name)
                return ctx.do_once("rollback", lambda: "undo-" + name)
            return Step(name, action, requires=[] if name == "a" else
                        [chr(ord(name) - 1)], rollback=rb)

        steps = [mk("a"), mk("b"), mk("c")]
        steps.append(Step("boom", lambda _c: (_ for _ in ()).throw(
            RuntimeError("x")), requires=["c"]))
        result = self.executor(steps, {"cpu": 4}).run()
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.execution_order, ["a", "b", "c", "boom"])
        self.assertEqual(rolled, ["c", "b", "a"])  # 严格逆序
        rb_ids = [r.id for r in result.rollback]
        self.assertEqual(rb_ids, ["boom", "c", "b", "a"])

    def test_non_retriable_fails_immediately(self):
        calls = {"n": 0}

        def fail(_ctx):
            calls["n"] += 1
            raise RuntimeError("no retry")

        steps = [Step("a", fail, retriable=True,
                      retry_policy=RetryPolicy(max_attempts=5))]
        result = self.executor(steps, {"cpu": 1}).run()
        self.assertEqual(calls["n"], 5)
        # 未标记 retriable 时策略不生效
        calls["n"] = 0
        shutil.rmtree(self.dir)
        os.makedirs(self.dir)
        steps = [Step("a", fail, retry_policy=RetryPolicy(max_attempts=5))]
        result = self.executor(steps, {"cpu": 1}).run()
        self.assertEqual(calls["n"], 1)
        self.assertEqual(result.status, "FAILED")


class TestResume(Base):
    def test_halt_and_resume_matches_uninterrupted(self):
        # 第一次运行：b 执行前请求暂停 => 停在断点
        def halt_action(_ctx):
            raise HaltRun()

        steps_v1 = [
            Step("a", lambda ctx: ctx.do_once("e", lambda: "A")),
            Step("b", halt_action, requires=["a"]),
            Step("c", lambda ctx: ctx.do_once("e", lambda: "C"),
                 requires=["b"]),
        ]
        r1 = self.executor(steps_v1, {"cpu": 4}).run()
        self.assertEqual(r1.status, "HALTED")
        self.assertEqual(r1.steps["a"].status, "SUCCESS")
        self.assertEqual(r1.steps["b"].status, "PENDING")

        # 断点续跑：同一 state 文件，b 换成正常实现
        steps_v2 = [
            Step("a", lambda ctx: ctx.do_once("e", lambda: "A")),
            Step("b", lambda ctx: ctx.do_once("e", lambda: "B"),
                 requires=["a"]),
            Step("c", lambda ctx: ctx.do_once("e", lambda: "C"),
                 requires=["b"]),
        ]
        r2 = Executor(steps_v2, {"cpu": 4}, self.state,
                      sleeper=lambda _s: None).run()
        self.assertEqual(r2.status, "SUCCESS")
        self.assertEqual(r2.execution_order, ["a", "b", "c"])

        # 对照组：全新 state 不中断执行
        fresh_dir = tempfile.mkdtemp(prefix="orch-fresh-")
        try:
            fresh_state = os.path.join(fresh_dir, "checkpoint.json")
            r3 = Executor(steps_v2, {"cpu": 4}, fresh_state,
                          sleeper=lambda _s: None).run()
            self.assertEqual(r3.status, "SUCCESS")
            keyify = lambda ledger: sorted(
                (e["step"], e["key"], e.get("result")) for e in ledger)
            self.assertEqual(keyify(r2.effect_ledger),
                             keyify(r3.effect_ledger),
                             "断点续跑的副作用必须与不中断执行一致")
            self.assertEqual(r2.execution_order, r3.execution_order)
        finally:
            shutil.rmtree(fresh_dir, ignore_errors=True)

    def test_crash_resume_via_subprocess(self):
        """子进程在步骤副作用后崩溃 => 续跑不产生重复副作用，结果与不中断一致。"""
        child_code = r'''
import os, sys
from orchestrator import Executor, Step

def a(ctx):
    return ctx.do_once("effect", lambda: "A")

def b(ctx):
    out = ctx.do_once("effect", lambda: "B")   # 副作用已产生
    os._exit(2)                                # 随即崩溃，SUCCESS 未落盘

steps = [Step("a", a), Step("b", b, requires=["a"]),
         Step("c", lambda ctx: ctx.do_once("effect", lambda: "C"),
              requires=["b"])]
Executor(steps, {"cpu": 4}, sys.argv[1], sleeper=lambda s: None).run()
'''
        child = os.path.join(self.dir, "child.py")
        with open(child, "w", encoding="utf-8") as fh:
            fh.write(child_code)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [os.getcwd(), env.get("PYTHONPATH", "")])
        proc = subprocess.run([sys.executable, child, self.state],
                              capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        snap = json.load(open(self.state, encoding="utf-8"))
        self.assertEqual(snap["steps"]["b"]["status"], "RUNNING")

        # 断点续跑：b 不再崩溃
        def b_ok(ctx):
            return ctx.do_once("effect", lambda: "B")

        steps = [
            Step("a", lambda ctx: ctx.do_once("effect", lambda: "A")),
            Step("b", b_ok, requires=["a"]),
            Step("c", lambda ctx: ctx.do_once("effect", lambda: "C"),
                 requires=["b"]),
        ]
        r2 = Executor(steps, {"cpu": 4}, self.state,
                      sleeper=lambda _s: None).run()
        self.assertEqual(r2.status, "SUCCESS")
        b_effects = [e for e in r2.effect_ledger
                     if e["step"] == "b" and e["key"] == "effect"]
        self.assertEqual(len(b_effects), 1, "崩溃重跑不得重复副作用")

        # 与不中断执行对比
        fresh_dir = tempfile.mkdtemp(prefix="orch-fresh-")
        try:
            r3 = Executor(steps, {"cpu": 4},
                          os.path.join(fresh_dir, "checkpoint.json"),
                          sleeper=lambda _s: None).run()
            keyify = lambda ledger: sorted(
                (e["step"], e["key"], e.get("result")) for e in ledger)
            self.assertEqual(keyify(r2.effect_ledger),
                             keyify(r3.effect_ledger))
            self.assertEqual(r2.execution_order, r3.execution_order)
        finally:
            shutil.rmtree(fresh_dir, ignore_errors=True)


class TestReport(Base):
    def test_slow_step_flagged_in_report(self):
        def slow(_ctx):
            time.sleep(0.2)

        steps = [Step("fast", noop, duration_estimate=0.01),
                 Step("slow", slow, requires=["fast"],
                      duration_estimate=0.01)]
        result = self.executor(steps, {"cpu": 2},
                               slow_min_delta=0.05).run()
        self.assertEqual(result.status, "SUCCESS")
        slow_ids = [s["id"] for s in result.slow_steps]
        self.assertEqual(slow_ids, ["slow"])

    def test_report_json_serializable(self):
        from orchestrator.report import build_report
        steps = [Step("a", lambda ctx: ctx.do_once("k", lambda: "v"))]
        executor = self.executor(steps, {"cpu": 1})
        result = executor.run()
        report = build_report(result, executor.plan)
        json.dumps(report)  # 不抛异常即可
        self.assertTrue(report["order"]["matches_plan"])
        self.assertFalse(report["rollback"]["triggered"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
