#!/usr/bin/env python3
"""演示：一次发布编排 5 个组件，含执行/回滚计划、续跑、幂等、差异报告。

用法:
  python3 example_run.py normal      # 全部成功
  python3 example_run.py flaky       # 可重试步骤抖动，第 3 次成功，副作用仅 1 次
  python3 example_run.py fail        # 步骤重试耗尽 -> 跳过下游 -> 逆序回滚
  python3 example_run.py crash-demo  # 子进程在步骤中途崩溃 -> 断点续跑，结果与不中断一致
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

from orchestrator import Executor, Step
from orchestrator.model import RetryPolicy
from orchestrator.planner import Planner
from orchestrator.report import build_report, render_text, save_report

# 资源上限：migrate 需要 2；deploy_web/deploy_worker 各需 3，
# 上限只有 3，因此二者在计划中必须串行。
LIMITS = {"cpu": 3.0}


def _effect(ctx, name, payload=None):
    """所有对外副作用都走幂等原语：同一 (step, key) 只产生一次。"""
    return ctx.do_once("effect", lambda: name)


def _rollback_effect(ctx, name):
    return ctx.do_once("rollback", lambda: "undo-" + name)


def make_steps(flaky=False, slow=False, failing=False):
    def prepare(ctx):
        out = _effect(ctx, "prepare")
        ctx.do_once("side:download", lambda: "package.tar.gz")
        return out

    def migrate(ctx):
        return _effect(ctx, "migrate_db")

    def deploy_web(ctx):
        return _effect(ctx, "deploy_web")

    def deploy_worker(ctx):
        return _effect(ctx, "deploy_worker")

    def switch(ctx):
        return _effect(ctx, "switch_traffic")

    steps = [
        Step("prepare", prepare, resources={"cpu": 1},
             duration_estimate=0.02,
             rollback=lambda ctx: _rollback_effect(ctx, "prepare")),
        Step("migrate_db", migrate, requires=["prepare"],
             resources={"cpu": 2}, duration_estimate=0.03,
             rollback=lambda ctx: _rollback_effect(ctx, "migrate_db")),
        Step("deploy_web", deploy_web, requires=["migrate_db"],
             resources={"cpu": 3}, duration_estimate=0.04,
             rollback=lambda ctx: _rollback_effect(ctx, "deploy_web")),
        Step("deploy_worker", deploy_worker, requires=["migrate_db"],
             resources={"cpu": 3}, duration_estimate=0.04,
             rollback=lambda ctx: _rollback_effect(ctx, "deploy_worker")),
        Step("switch_traffic", switch,
             requires=["deploy_web", "deploy_worker"],
             resources={"cpu": 1}, duration_estimate=0.02,
             rollback=lambda ctx: _rollback_effect(ctx, "switch_traffic")),
    ]
    if flaky:
        # 前两次抛错，第三次成功；副作用仍只能有一次
        state = {"n": 0}

        def deploy_web_flaky(ctx):
            state["n"] += 1
            if state["n"] < 3:
                raise RuntimeError("服务还没起来(第%d次)" % state["n"])
            return _effect(ctx, "deploy_web")

        steps[2] = Step(
            "deploy_web", deploy_web_flaky, requires=["migrate_db"],
            resources={"cpu": 3}, duration_estimate=0.04,
            retriable=True, retry_policy=RetryPolicy(max_attempts=3),
            rollback=lambda ctx: _rollback_effect(ctx, "deploy_web"))

    if slow or failing:
        # 模拟耗时异常：实际 0.25s，预估 0.04s
        def deploy_web_slow(ctx):
            time.sleep(0.25)
            if failing:
                raise RuntimeError("健康检查未通过")
            return _effect(ctx, "deploy_web")

        steps[2] = Step(
            "deploy_web", deploy_web_slow, requires=["migrate_db"],
            resources={"cpu": 3}, duration_estimate=0.04,
            retriable=True, retry_policy=RetryPolicy(max_attempts=2),
            rollback=lambda ctx: _rollback_effect(ctx, "deploy_web"))
    return steps


def run_once(workdir, scenario, resume=False):
    os.makedirs(workdir, exist_ok=True)
    state_path = os.path.join(workdir, "checkpoint.json")
    flaky = scenario == "flaky"
    slow = scenario == "fail"
    executor = Executor(
        make_steps(flaky=flaky, slow=slow, failing=slow), LIMITS, state_path,
        max_workers=4, sleeper=lambda _s: None)

    if not resume:
        crash_at = os.environ.get("CRASH_AT")
        if crash_at:
            # 在指定步骤的副作用产生之后、状态落盘 SUCCESS 之前崩溃
            original = executor.steps[crash_at].action

            def crashing(ctx, _original=original, _step=crash_at):
                result = _original(ctx)
                sys.stderr.write("[child] 在 %s 副作用后崩溃 os._exit(2)\n" % _step)
                sys.stderr.flush()
                os._exit(2)
                return result

            executor.steps[crash_at].action = crashing

    result = executor.run()
    report = build_report(result, executor.plan)
    save_report(report, os.path.join(workdir, "report.json"))
    return executor, result, report


def print_plan():
    planner = Planner(make_steps(), LIMITS)
    plan = planner.make_plan()
    print("== 执行计划 ==")
    print("派发顺序(拓扑序):", " -> ".join(plan.order))
    for wave_no, wave in enumerate(plan.waves):
        starts = {e["id"]: e["start"] for e in plan.timeline}
        detail = ", ".join("%s@t=%.2f" % (sid, starts[sid]) for sid in wave)
        print("  wave %d: %s" % (wave_no, detail))
    print("预计总工期: %.2fs" % plan.estimated_makespan)
    print("回滚计划(按执行顺序逆序):",
          " -> ".join(Planner.rollback_order(plan.order)))
    print()


def assert_ledger_equivalent(ledger_a, ledger_b, label):
    """忽略 seq 后副作用账本必须一致 => 中断恢复没有重复/遗漏副作用。"""
    keyify = lambda ledger: sorted(
        (entry["step"], entry["key"], entry.get("result"))
        for entry in ledger)
    a, b = keyify(ledger_a), keyify(ledger_b)
    assert a == b, "账本不一致(%s):\n%r\n!=\n%r" % (label, a, b)
    print("[断言] 副作用账本一致(%s)，共 %d 条" % (label, len(a)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario",
                        choices=["normal", "flaky", "fail", "crash-demo"])
    parser.add_argument("--workdir", default="demo_output")
    args = parser.parse_args()
    print_plan()

    if args.scenario == "crash-demo":
        return crash_demo(args.workdir)

    workdir = os.path.join(args.workdir, args.scenario)
    shutil.rmtree(workdir, ignore_errors=True)
    executor, result, report = run_once(workdir, args.scenario)
    print(render_text(report))
    print()

    if args.scenario == "normal":
        assert result.status == "SUCCESS"
        assert all(r.status == "SUCCESS" for r in result.steps.values())
        print("[断言] 全部步骤成功")

    if args.scenario == "flaky":
        assert result.status == "SUCCESS"
        rep = result.steps["deploy_web"]
        assert rep.attempts == 3, rep.attempts
        effects = [e for e in result.effect_ledger
                   if e["step"] == "deploy_web" and e["key"] == "effect"]
        assert len(effects) == 1, "重试导致重复副作用: %r" % effects
        print("[断言] deploy_web 重试 3 次后成功；幂等副作用只产生 1 次")

    if args.scenario == "fail":
        assert result.status == "FAILED"
        assert result.steps["deploy_web"].attempts == 2
        print("[断言] deploy_web 重试达到上限(2次)后进入失败处理")
        exec_order = result.execution_order
        rb_ids = [rec.id for rec in result.rollback if rec.status != "NOOP"]
        assert rb_ids == list(reversed(exec_order)), (rb_ids, exec_order)
        print("[断言] 回滚顺序为已执行步骤的严格逆序:")
        print("       执行: %s" % " -> ".join(exec_order))
        print("       回滚: %s" % " -> ".join(rb_ids))
        assert "deploy_worker" in result.skipped
        assert "switch_traffic" in result.skipped
        print("[断言] 下游 deploy_worker / switch_traffic 被跳过")
        assert result.slow_steps and result.slow_steps[0]["id"] == "deploy_web"
        print("[断言] 差异报告标记 deploy_web 耗时异常")
    print("\n报告已写入:", os.path.join(workdir, "report.json"))
    return 0


def crash_demo(base):
    """两阶段：子进程崩溃 -> 同一份 checkpoint 续跑；再与不中断运行对比。"""
    workdir = os.path.join(base, "crash")
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir)
    state = os.path.join(workdir, "checkpoint.json")

    env = dict(os.environ, CRASH_AT="deploy_worker")
    code = ("from example_run import run_once;"
            "run_once(%r, 'normal')" % workdir)
    proc = subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 2, proc.returncode
    print("[阶段1] 子进程在 deploy_worker 副作用后崩溃(exit=2)")
    snap = json.load(open(state, encoding="utf-8"))
    assert snap["steps"]["deploy_worker"]["status"] == "RUNNING"
    print("[断言] 断点已落盘，deploy_worker 停留在 RUNNING")

    code = ("from example_run import run_once;"
            "e,r,rep=run_once(%r, 'normal', resume=True);"
            "from orchestrator.report import save_report;"
            "save_report(rep, %r)" % (
                workdir, os.path.join(workdir, "report.json")))
    proc = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
        raise AssertionError("续跑失败")
    print("[阶段2] 使用同一份 checkpoint 断点续跑完成")

    crashed = json.load(open(state, encoding="utf-8"))
    assert crashed["status"] == "SUCCESS"
    dw_effects = [e for e in crashed["ledger"]
                  if e["step"] == "deploy_worker" and e["key"] == "effect"]
    assert len(dw_effects) == 1, dw_effects
    print("[断言] 续跑后整体成功；崩溃步骤的副作用未重复(仅 1 条)")

    # 对照组：全新目录不中断运行
    fresh = os.path.join(base, "crash_fresh")
    shutil.rmtree(fresh, ignore_errors=True)
    code = ("from example_run import run_once;"
            "e,r,rep=run_once(%r, 'normal')" % fresh)
    subprocess.run([sys.executable, "-c", code], check=True)
    fresh_state = json.load(open(os.path.join(fresh, "checkpoint.json"),
                                 encoding="utf-8"))
    assert_ledger_equivalent(crashed["ledger"], fresh_state["ledger"],
                             "崩溃续跑 vs 不中断")
    crashed_statuses = {k: v["status"] for k, v in crashed["steps"].items()}
    fresh_statuses = {k: v["status"] for k, v in
                      fresh_state["steps"].items()}
    assert crashed_statuses == fresh_statuses
    print("[断言] 各步骤最终状态与不中断执行完全一致")
    print("\n报告已写入:", os.path.join(workdir, "report.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
