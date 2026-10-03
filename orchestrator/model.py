"""数据模型：资源、步骤、策略、执行结果。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# ---- 步骤状态机 ----
PENDING = "PENDING"
RUNNING = "RUNNING"
SUCCESS = "SUCCESS"
FAILED = "FAILED"
SKIPPED = "SKIPPED"               # 依赖失败导致未执行
ROLLED_BACK = "ROLLED_BACK"       # 已执行回滚
ROLLBACK_FAILED = "ROLLBACK_FAILED"

TERMINAL = {SUCCESS, FAILED, SKIPPED, ROLLED_BACK, ROLLBACK_FAILED}
# 回滚时需要处理（执行过正操作）的状态
EXECUTED_STATES = {SUCCESS, FAILED}


@dataclass
class Resources:
    """多维度资源配额，例如 cpu/mem/gpu/连接数。"""
    limits: Dict[str, float]

    def __post_init__(self) -> None:
        for key, value in self.limits.items():
            if value < 0:
                raise ValueError("资源上限不能为负: %s=%r" % (key, value))

    @staticmethod
    def fits(used: Dict[str, float], need: Dict[str, float],
             limits: Dict[str, float]) -> bool:
        for key, value in need.items():
            if used.get(key, 0.0) + value > limits.get(key, 0.0) + 1e-9:
                return False
        return True


@dataclass
class RetryPolicy:
    max_attempts: int = 1          # 含首次，1 表示不重试
    backoff_seconds: float = 0.0   # 固定退避（可注入为 0 加速测试）

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts 必须 >= 1")
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds 不能为负")


@dataclass
class Step:
    id: str
    action: Callable[["Context"], None]
    requires: List[str] = field(default_factory=list)        # 依赖的步骤
    resources: Dict[str, float] = field(default_factory=dict)
    duration_estimate: float = 1.0     # 仅用于离线计划
    retriable: bool = False
    retry_policy: Optional[RetryPolicy] = None
    rollback: Optional[Callable[["Context"], None]] = None  # 补偿动作
    note: str = ""

    def policy(self) -> RetryPolicy:
        return self.retry_policy or RetryPolicy()


@dataclass
class Context:
    """传给 action / rollback 的运行时句柄。

    - success_outputs: 已成功步骤的返回值
    - do_once(key, fn): 幂等副作用原语，进程崩溃恢复后也不会重复
    """
    step_id: str
    success_outputs: Dict[str, Any]
    idempotency: "IdempotencyStore"

    def do_once(self, key: str, fn: Callable[[], Any]) -> Any:
        return self.idempotency.get_or_run(self.step_id, key, fn)


@dataclass
class StepReport:
    id: str
    status: str
    attempts: int = 0
    duration: float = 0.0
    planned_wave: int = -1
    planned_start: float = 0.0
    skipped_reason: str = ""
    error: str = ""
    rollback_status: str = ""   # 未回滚为 ""，否则 ROLLED_BACK/ROLLBACK_FAILED/NOOP


@dataclass
class RollbackRecord:
    id: str
    status: str
    duration: float = 0.0
    error: str = ""


@dataclass
class RunResult:
    status: str                      # SUCCESS / FAILED / HALTED
    steps: Dict[str, StepReport]
    rollback: List[RollbackRecord]
    skipped: List[str]
    timeline: List[Dict[str, Any]]
    slow_steps: List[Dict[str, Any]]
    plan_order: List[str]
    execution_order: List[str]
    effect_ledger: List[Dict[str, Any]]
