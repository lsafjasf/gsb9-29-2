"""执行器：按派发顺序调度，支持暂停/断点续跑/幂等重试/逆序回滚。"""
from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Callable, Dict, List, Optional

from .model import (Context, EXECUTED_STATES, FAILED, PENDING, ROLLED_BACK,
                    ROLLBACK_FAILED, RUNNING, SKIPPED, SUCCESS, Resources,
                    RunResult, Step, StepReport, RollbackRecord)
from .planner import Plan, Planner
from .report import slow_steps as compute_slow_steps
from .state import CheckpointStore, IdempotencyStore


class HaltRun(Exception):
    """请求暂停：等待在途步骤完成后安全退出，断点已落盘。"""


class Executor:
    def __init__(self,
                 steps: List[Step],
                 limits: Dict[str, float],
                 state_path: str,
                 max_workers: int = 8,
                 sleeper: Callable[[float], None] = time.sleep,
                 slow_factor: float = 3.0,
                 slow_min_delta: float = 0.05) -> None:
        self.steps = {s.id: s for s in steps}
        self.limits = dict(limits)
        self.max_workers = max(1, max_workers)
        self.sleeper = sleeper
        self.slow_factor = slow_factor
        self.slow_min_delta = slow_min_delta
        self.checkpoint = CheckpointStore(state_path)
        self.idempotency = IdempotencyStore(self.checkpoint)
        self.plan: Plan = Planner(steps, limits).make_plan()
        self._planned_wave = {e["id"]: e["wave"] for e in self.plan.timeline}
        self._planned_start = {e["id"]: e["start"] for e in self.plan.timeline}
        self._started = False

    # ---- 对外入口 ----
    def run(self) -> RunResult:
        if not self._started:
            self.checkpoint.load_or_init(list(self.steps))
            # 加载已有断点（含新进程恢复崩溃）后，恢复停留在 RUNNING 的步骤
            recovered = self.checkpoint.recover_stale_running()
            for sid in recovered:
                self.checkpoint.log_event("recovered", step=sid)
            self._started = True
        try:
            self._execute()
        except HaltRun:
            self.checkpoint.set_run_status("HALTED")
            return self._result("HALTED")
        return self._result(self.checkpoint.data["status"])

    # ---- 主调度循环 ----
    def _execute(self) -> None:
        store = self.checkpoint
        store.set_run_status("RUNNING")
        failure: Optional[str] = None
        halt_requested = False
        running: Dict[Any, str] = {}
        used: Dict[str, float] = {key: 0.0 for key in self.limits}

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            while True:
                # 派发：严格按计划的派发顺序，资源允许且依赖就绪才提交
                for sid in self.plan.order:
                    info = store.step_info(sid)
                    if info["status"] != PENDING:
                        continue
                    if failure is not None or halt_requested:
                        continue
                    dep_failed = any(
                        store.step_status(dep) in (FAILED, SKIPPED)
                        for dep in self.steps[sid].requires)
                    if dep_failed:
                        reason = "依赖失败，跳过"
                        store.update_step(sid, status=SKIPPED,
                                          skipped_reason=reason)
                        store.log_event("skipped", step=sid, reason=reason)
                        continue
                    if not all(store.step_status(dep) == SUCCESS
                               for dep in self.steps[sid].requires):
                        continue
                    need = self.steps[sid].resources
                    if len(running) >= self.max_workers or not Resources.fits(
                            used, need, self.limits):
                        continue
                    for key, value in need.items():
                        used[key] = used.get(key, 0.0) + value
                    future = pool.submit(self._run_step, sid)
                    running[future] = sid

                if not running:
                    break
                done, _ = wait(running.keys(), return_when=FIRST_COMPLETED)
                for future in done:
                    sid = running.pop(future)
                    for key, value in self.steps[sid].resources.items():
                        used[key] -= value
                    outcome = future.result()  # _run_step 内部不抛业务异常
                    if outcome == "FAILED" and failure is None:
                        failure = sid
                    elif outcome == "HALTED":
                        halt_requested = True

        if halt_requested:
            raise HaltRun()
        if failure is not None:
            self._skip_remaining()
            self._rollback()
            store.set_run_status("FAILED")
        else:
            store.set_run_status("SUCCESS")

    # ---- 单步执行（含重试）----
    def _run_step(self, sid: str) -> str:
        step = self.steps[sid]
        store = self.checkpoint
        policy = step.policy()
        max_attempts = policy.max_attempts if step.retriable else 1
        attempts = store.step_info(sid)["attempts"]
        attempts_before = attempts
        store.update_step(sid, status=RUNNING)
        store.log_event("step_start", step=sid, attempt=attempts + 1)
        started = time.monotonic()
        last_error = ""
        while attempts < max_attempts:
            attempts += 1
            store.update_step(sid, attempts=attempts)
            try:
                ctx = Context(sid, store.data["outputs"], self.idempotency)
                output = step.action(ctx)
                duration = time.monotonic() - started
                store.record_output(sid, output)
                store.update_step(sid, status=SUCCESS, duration=duration)
                store.log_event("step_end", step=sid, status=SUCCESS,
                                duration=round(duration, 6),
                                attempts=attempts)
                return "SUCCESS"
            except HaltRun:
                # 暂停请求：保持 PENDING，本次尝试不消耗重试预算，等待续跑
                store.update_step(sid, status=PENDING,
                                  attempts=attempts_before)
                store.log_event("halted", step=sid)
                return "HALTED"
            except Exception as exc:  # noqa: BLE001 - 业务异常转为步骤失败
                last_error = "%s: %s" % (type(exc).__name__, exc)
                store.log_event("step_attempt_failed", step=sid,
                                attempt=attempts, error=last_error)
                if attempts < max_attempts:
                    store.log_event("retry", step=sid, attempt=attempts + 1)
                    self.sleeper(policy.backoff_seconds)
        duration = time.monotonic() - started
        store.update_step(sid, status=FAILED, duration=duration,
                          error=last_error)
        store.log_event("step_end", step=sid, status=FAILED,
                        duration=round(duration, 6), attempts=attempts,
                        error=last_error)
        return "FAILED"

    # ---- 失败传播与回滚 ----
    def _skip_remaining(self) -> None:
        store = self.checkpoint
        for sid in self.plan.order:
            if store.step_status(sid) == PENDING:
                reason = "上游失败，未执行"
                store.update_step(sid, status=SKIPPED, skipped_reason=reason)
                store.log_event("skipped", step=sid, reason=reason)

    def _rollback(self) -> None:
        """回滚：已执行步骤（成功或失败）按执行顺序的逆序逐个补偿。"""
        store = self.checkpoint
        executed = [e["step"] for e in store.data["events"]
                    if e["kind"] == "step_start"]
        # 去重但保留首次执行顺序
        seen = set()
        executed = [sid for sid in executed
                    if not (sid in seen or seen.add(sid))]
        targets = [sid for sid in reversed(executed)
                   if store.step_status(sid) in EXECUTED_STATES]
        for sid in targets:
            step = self.steps[sid]
            store.log_event("rollback_start", step=sid)
            started = time.monotonic()
            record: Dict[str, Any] = {"id": sid}
            if step.rollback is None:
                store.update_step(sid, rollback_status="NOOP")
                record.update(status="NOOP", duration=0.0)
                store.log_event("rollback_end", step=sid, status="NOOP")
            else:
                try:
                    ctx = Context(sid, store.data["outputs"], self.idempotency)
                    step.rollback(ctx)
                    duration = time.monotonic() - started
                    store.update_step(sid, rollback_status=ROLLED_BACK)
                    record.update(status=ROLLED_BACK,
                                  duration=round(duration, 6))
                    store.log_event("rollback_end", step=sid,
                                    status=ROLLED_BACK,
                                    duration=round(duration, 6))
                except Exception as exc:  # noqa: BLE001
                    duration = time.monotonic() - started
                    error = "%s: %s" % (type(exc).__name__, exc)
                    store.update_step(sid, rollback_status=ROLLBACK_FAILED)
                    record.update(status=ROLLBACK_FAILED,
                                  duration=round(duration, 6), error=error)
                    store.log_event("rollback_end", step=sid,
                                    status=ROLLBACK_FAILED, error=error)
            store.record_rollback(record)

    # ---- 结果汇总 ----
    def _result(self, status: str) -> RunResult:
        store = self.checkpoint
        steps: Dict[str, StepReport] = {}
        for sid, info in store.data["steps"].items():
            steps[sid] = StepReport(
                id=sid, status=info["status"],
                attempts=info["attempts"], duration=info["duration"],
                planned_wave=self._planned_wave.get(sid, -1),
                planned_start=self._planned_start.get(sid, 0.0),
                skipped_reason=info.get("skipped_reason", ""),
                error=info.get("error", ""),
                rollback_status=info.get("rollback_status", ""))
        rollback = [RollbackRecord(**rec)
                    for rec in store.data["rollback"]]
        skipped = [sid for sid, rep in steps.items()
                   if rep.status == SKIPPED]
        execution_order = []
        seen = set()
        for event in store.data["events"]:
            if event["kind"] == "step_start" and event["step"] not in seen:
                seen.add(event["step"])
                execution_order.append(event["step"])
        slow = compute_slow_steps(
            steps, self.steps, self.slow_factor, self.slow_min_delta)
        return RunResult(
            status=status, steps=steps, rollback=rollback, skipped=skipped,
            timeline=store.data["events"], slow_steps=slow,
            plan_order=list(self.plan.order),
            execution_order=execution_order,
            effect_ledger=list(store.data["ledger"]))
