"""执行器：逐步执行、检查点续跑、幂等重试、失败逆序回滚。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Mapping, Optional

from .model import Step, World, effect_key
from .planner import Plan, normalize_steps

STATE_VERSION = 1


class StepError(Exception):
    def __init__(self, step_name: str, attempt: int) -> None:
        self.step_name = step_name
        self.attempt = attempt
        super().__init__(f"步骤 {step_name!r} 第 {attempt} 次尝试失败")


@dataclass
class StepRecord:
    name: str
    attempts: int
    start: float
    end: float
    status: str  # completed / failed / skipped / rolled_back


@dataclass
class RunResult:
    status: str  # success / failed / paused
    executed: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    rolled_back: List[str] = field(default_factory=list)
    records: Dict[str, StepRecord] = field(default_factory=dict)
    clock: float = 0.0


def _fresh_state() -> dict:
    return {
        "version": STATE_VERSION,
        "status": "running",
        "wave_index": 0,
        "clock": 0.0,
        "completed": [],
        "executed": [],
        "attempts": {},
        "records": {},
        "skipped": [],
        "rolled_back": [],
    }


class Executor:
    """按计划执行。

    state_path 给定时，每一步的结果都原子落盘；重建 Executor 即可续跑。
    World 在真实环境中就是被部署的基础设施；测试中用同一个实例承载。
    副作用通过幂等键去重，重试/重复执行不会产生第二次副作用。
    """

    def __init__(
        self,
        plan: Plan,
        steps: Mapping[str, Step],
        world: World,
        state_path: Optional[str] = None,
        retry_delay: float = 0.5,
    ) -> None:
        self.plan = plan
        self.steps = normalize_steps(steps)
        self.world = world
        self.state_path = state_path
        self.retry_delay = retry_delay
        self.state = self._load()

    # ---------- 检查点 ----------

    def _load(self) -> dict:
        if self.state_path and os.path.exists(self.state_path):
            with open(self.state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            if state.get("version") != STATE_VERSION:
                raise ValueError(f"不兼容的状态版本: {state.get('version')!r}")
            if state["status"] == "failed":
                raise RuntimeError("该任务已失败并完成回滚，不可直接续跑；请重置后重发")
            return state
        return _fresh_state()

    def _save(self) -> None:
        if not self.state_path:
            return
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    @classmethod
    def reset(cls, state_path: str) -> None:
        """删除检查点，允许重新发布。"""
        if os.path.exists(state_path):
            os.remove(state_path)

    # ---------- 执行 ----------

    def _attempt(self, step: Step, attempt_no: int) -> None:
        """单次尝试：副作用先经幂等键生效，再模拟可能的崩溃/失败。"""
        self.world.apply(effect_key(step.name), {"step": step.name, "attempt": attempt_no})
        if step.always_fail or attempt_no <= step.fail_times:
            raise StepError(step.name, attempt_no)

    def _run_step(self, step: Step, wave_start: float) -> StepRecord:
        attempts = self.state["attempts"].get(step.name, 0)
        retry_time = 0.0
        while True:
            attempts += 1
            self.state["attempts"][step.name] = attempts
            self._save()
            try:
                self._attempt(step, attempts)
                break
            except StepError:
                retries_used = attempts - 1
                if step.retryable and retries_used < step.max_retries:
                    retry_time += self.retry_delay
                    continue
                record = StepRecord(
                    step.name, attempts, wave_start, wave_start + retry_time, "failed"
                )
                self.state["records"][step.name] = asdict(record)
                self._save()
                return record

        record = StepRecord(
            step.name,
            attempts,
            wave_start,
            wave_start + retry_time + step.effective_duration(),
            "completed",
        )
        self.state["records"][step.name] = asdict(record)
        self.state["completed"].append(step.name)
        self.state["executed"].append(step.name)
        self._save()
        return record

    def _rollback(self, failed_wave_index: int, failed_name: str) -> None:
        done = set(self.state["completed"])
        self.state["skipped"] = [
            name
            for wave in self.plan.waves[failed_wave_index:]
            for name in wave.steps
            if name not in done and name != failed_name
        ]
        # 先补偿失败步骤自身可能已生效的半成品副作用，再逆序补偿已完成步骤
        to_compensate = []
        if effect_key(failed_name) in self.world.applied:
            to_compensate.append(failed_name)
        to_compensate.extend(reversed(self.state["executed"]))
        for name in to_compensate:
            self.world.revert(effect_key(name))
            self.state["rolled_back"].append(name)
            if name in self.state["records"]:
                if self.state["records"][name]["status"] == "completed":
                    self.state["records"][name]["status"] = "rolled_back"
        self.state["status"] = "failed"
        self._save()

    def run(self, pause_after: Optional[int] = None) -> RunResult:
        """执行计划。

        pause_after: 成功完成这么多步后暂停（模拟进程退出），检查点已落盘；
        再次构造 Executor 并 run() 即从断点续跑。
        """
        st = self.state
        wave_index = st["wave_index"]
        while wave_index < len(self.plan.waves):
            wave = self.plan.waves[wave_index]
            wave_start = st["clock"]
            # 续跑时从检查点记录恢复本波已完成步骤到达的最晚时刻，
            # 保证后续步骤的起始时间与一次跑完一致
            wave_end = max(
                (
                    st["records"][n]["end"]
                    for n in wave.steps
                    if n in st["records"]
                    and st["records"][n]["status"] == "completed"
                ),
                default=wave_start,
            )
            for name in wave.steps:
                if name in st["completed"]:
                    continue
                record = self._run_step(self.steps[name], wave_start)
                wave_end = max(wave_end, record.end)
                if record.status == "failed":
                    self._rollback(wave_index, record.name)
                    return self._result("failed")
                if pause_after is not None and len(st["executed"]) >= pause_after:
                    # 波次内时钟不前进，剩余步骤续跑时仍从波首时刻开始，
                    # 因此结果与一次跑完完全一致。
                    st["status"] = "paused"
                    self._save()
                    return self._result("paused")
            st["clock"] = wave_end
            wave_index += 1
            st["wave_index"] = wave_index
            self._save()

        st["status"] = "success"
        self._save()
        return self._result("success")

    def _result(self, status: str) -> RunResult:
        records = {
            name: StepRecord(**data)
            for name, data in self.state["records"].items()
        }
        return RunResult(
            status=status,
            executed=list(self.state["executed"]),
            skipped=list(self.state["skipped"]),
            rolled_back=list(self.state["rolled_back"]),
            records=records,
            clock=self.state["clock"],
        )
