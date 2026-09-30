"""数据模型：发布步骤（组件）、资源容量、以及模拟真实系统副作用的 World。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class Step:
    """一个待发布组件 / 步骤。

    name            唯一名称
    deps            必须先完成的步骤名（依赖边：deps -> name）
    resources       执行期间占用的资源 {资源名: 数量}
    duration        计划耗时（虚拟时钟单位）
    actual_duration 模拟的实际耗时；None 时等于 duration
    retryable       失败是否允许重试
    max_retries     最大重试次数（不含首次尝试）
    fail_times      模拟：前 N 次尝试在副作用生效之后失败（崩溃在提交之后）
    always_fail     模拟：每次尝试都失败
    """

    name: str
    deps: Tuple[str, ...] = ()
    resources: Dict[str, float] = field(default_factory=dict)
    duration: float = 1.0
    actual_duration: Optional[float] = None
    retryable: bool = True
    max_retries: int = 2
    fail_times: int = 0
    always_fail: bool = False

    def effective_duration(self) -> float:
        return self.actual_duration if self.actual_duration is not None else self.duration


def effect_key(step_name: str) -> str:
    """步骤副作用的幂等键。"""
    return f"deploy:{step_name}"


class World:
    """被发布系统修改的“真实世界”。

    所有副作用必须通过幂等键写入：同一键重复写入是无操作，
    因此步骤崩溃后重试不会重复产生副作用。
    revert 是对应补偿动作，对不存在的键同样幂等。
    """

    def __init__(self) -> None:
        self.applied: Dict[str, dict] = {}
        self.log: list = []  # 真实调用序列，便于断言

    def apply(self, key: str, payload: Optional[dict] = None) -> bool:
        if key in self.applied:
            self.log.append(("apply-skip", key))
            return False
        self.applied[key] = payload if payload is not None else {}
        self.log.append(("apply", key))
        return True

    def revert(self, key: str) -> bool:
        if key not in self.applied:
            self.log.append(("revert-skip", key))
            return False
        del self.applied[key]
        self.log.append(("revert", key))
        return True
