"""联合准入控制器。

核心思想：把处理器/内存/句柄等资源看成一个整体向量，准入判定是
"向量 <= 剩余向量" 的一次原子比较，而不是在三个独立资源管理器上
分别判定。这样不会出现"逐资源都能过、合起来超限"的情况。

防饥饿（见 schedule_locked）：
    FIFO 队列 + 老化屏障（aging barrier）+ 有界插队。
    - 排队者超过 starvation_threshold 后成为屏障，新人与队中后人
      都不能越过它进入（bounded overtaking）；
    - 屏障在资源够时立即被准入，否则占用判定位置等待；
    - 屏障之前的年轻排队者若恰好装得下可以先进入，因此插队次数
      受队列中屏障数量约束，任何任务等待时间有上界。

防死锁：
    "请求所有资源，要么全给要么一个不给"，任务拿到全部申请资源后
    才成为持有者；持有者释放只减少占用，从不阻塞等待新资源
    （禁止边持有边追加申请）。因此 hold-and-wait 与 circular wait
    两个 Coffman 必要条件都不成立，系统无死锁。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


class Vector(tuple):
    """不可变资源向量，按维度比较与加减。"""

    def __new__(cls, *components):
        if len(components) == 1 and not isinstance(components[0], (int, float, str, bytes)):
            components = tuple(components[0])
        values = tuple(int(c) for c in components)
        if not values:
            raise ValueError("资源向量至少需要一个维度")
        if any(v < 0 for v in values):
            raise ValueError("资源向量不允许负分量")
        return super().__new__(cls, values)

    def __add__(self, other):
        return Vector(a + b for a, b in zip(self, other))

    def __sub__(self, other):
        return Vector(a - b for a, b in zip(self, other))

    def fits(self, capacity: "Vector") -> bool:
        """self 的每个分量都不超过 capacity。"""
        return all(a <= b for a, b in zip(self, capacity))

    def positive_dims(self):
        return tuple(i for i, v in enumerate(self) if v > 0)

    @classmethod
    def zero(cls, n: int) -> "Vector":
        return cls((0,) * n)


class Rejected(Exception):
    """申请被拒绝：需求量超过总容量，或排队超时。"""

    def __init__(self, reason: str, request: Vector, wait: float = 0.0):
        super().__init__(reason)
        self.reason = reason
        self.request = request
        self.wait = wait


@dataclass
class _Waiter:
    request: Vector
    enqueued_at: float
    deadline: Optional[float]
    event: threading.Event = field(default_factory=threading.Event)
    granted: bool = False


class Grant:
    """一次成功准入得到的资源持有凭证，支持整体/部分释放。"""

    def __init__(self, controller: "AdmissionController", request: Vector,
                 granted_at: float):
        self._controller = controller
        self._request = request
        self._granted_at = granted_at
        self._remaining = request
        self._released = False

    @property
    def request(self) -> Vector:
        return self._request

    @property
    def remaining(self) -> Vector:
        return self._remaining

    @property
    def released(self) -> bool:
        return self._released

    def release(self, partial: Optional[Vector] = None) -> None:
        """释放全部（默认）或释放 partial 指定的部分资源。

        中途释放后不允许再次申请（整体准入模型）；部分释放可多次，
        释放量不能超过当前持有量。重复 release 幂等。
        """
        self._controller._return(self, partial)

    def held_duration(self, now: Optional[float] = None) -> float:
        if now is None:
            now = time.monotonic()
        return max(0.0, now - self._granted_at)

    def __enter__(self) -> "Grant":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class AdmissionController:
    def __init__(self, capacity, starvation_threshold: float = 0.15):
        capacity = capacity if isinstance(capacity, Vector) else Vector(capacity)
        if any(c <= 0 for c in capacity):
            raise ValueError("各维容量必须为正")
        self._capacity = capacity
        self._dim = len(capacity)
        self._usage = Vector.zero(self._dim)
        self._starvation_threshold = float(starvation_threshold)

        self._lock = threading.Lock()
        self._queue: deque[_Waiter] = deque()
        self._grants: dict[Grant, bool] = {}
        self._grant_seq = 0

        # 利用率：按维度累计 used * dt
        self._origin = time.monotonic()
        self._last_touch = self._origin
        self._used_area = [0.0] * self._dim

        # 统计
        self._total = 0
        self._admitted = 0
        self._rejected = 0
        self._rejected_over = 0
        self._rejected_timeout = 0
        self._sum_wait = 0.0
        self._sum_wait_sq = 0.0
        self._max_wait = 0.0
        self._wait_samples: list[float] = []
        self._sum_hold = 0.0

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _touch_locked(self, now: float) -> None:
        dt = now - self._last_touch
        if dt > 0:
            for i, u in enumerate(self._usage):
                self._used_area[i] += u * dt
            self._last_touch = now

    def _normalize(self, request) -> Vector:
        req = request if isinstance(request, Vector) else Vector(request)
        if len(req) != self._dim:
            raise ValueError(f"请求维度 {len(req)} 与容量维度 {self._dim} 不一致")
        return req

    def _admissible_locked(self, request: Vector, now: float) -> bool:
        """整体向量判定：当前已占用 + 本次 <= 容量（各维同时满足）。"""
        return (self._usage + request).fits(self._capacity)

    def _schedule_locked(self, now: float) -> None:
        """在释放/取消后尝试唤醒排队者。

        从队首开始扫描：
        * 已超时者直接清理；
        * 装得下的排队者被原子准入（从队列移除并承诺资源）；
        * 装不下且等待超过老化阈值的排队者成为屏障，其后一律不可越过；
        * 装不下但年轻的排队者可以被后面恰好装得下的任务越过（有界插队）。
        """
        carried: list[_Waiter] = []
        while self._queue:
            waiter = self._queue.popleft()
            if waiter.granted:
                continue
            if waiter.deadline is not None and waiter.deadline <= now:
                waiter.event.set()  # 唤醒后走拒绝路径
                continue
            if (self._usage + waiter.request).fits(self._capacity):
                self._commit_locked(waiter, now)
                waiter.event.set()
                continue
            if now - waiter.enqueued_at >= self._starvation_threshold:
                carried.append(waiter)
                break  # 老化屏障：后面的任务一律不可越过
            carried.append(waiter)

        for waiter in reversed(carried):
            self._queue.appendleft(waiter)

    def _commit_locked(self, waiter: _Waiter, now: float) -> None:
        waiter.granted = True
        self._usage = self._usage + waiter.request
        self._admitted += 1
        waited = max(0.0, now - waiter.enqueued_at)
        self._sum_wait += waited
        self._sum_wait_sq += waited * waited
        self._max_wait = max(self._max_wait, waited)
        self._wait_samples.append(waited)

    def _finish_rejection_locked(self, waiter: _Waiter, reason: str,
                                 now: float) -> Rejected:
        self._rejected += 1
        if reason == "over_capacity":
            self._rejected_over += 1
        else:
            self._rejected_timeout += 1
        waited = max(0.0, now - waiter.enqueued_at)
        self._sum_wait += waited
        self._sum_wait_sq += waited * waited
        self._max_wait = max(self._max_wait, waited)
        self._wait_samples.append(waited)
        return Rejected(reason, waiter.request, waited)

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #
    def acquire(self, request, timeout: Optional[float] = None) -> Grant:
        """申请整体资源向量。

        - request 任一维超过总容量：立即拒绝（reason="over_capacity"）；
        - timeout=None：一直排队直到拿到；
        - timeout=0：只尝试一次，拿不到立即拒绝（"timeout"）；
        - timeout>0：排队最多 timeout 秒，超时拒绝。
        """
        req = self._normalize(request)
        now = time.monotonic()
        with self._lock:
            self._touch_locked(now)
            self._total += 1

            if not req.fits(self._capacity):
                err = self._finish_rejection_locked(
                    _Waiter(req, now, now), "over_capacity", now)
                raise err

            deadline = None if timeout is None else now + float(timeout)
            if timeout is not None and timeout <= 0:
                if self._admissible_locked(req, now):
                    waiter = _Waiter(req, now, deadline)
                    self._commit_locked(waiter, now)
                else:
                    err = self._finish_rejection_locked(
                        _Waiter(req, now, now), "timeout", now)
                    raise err
            elif not self._queue and (self._usage + req).fits(self._capacity):
                waiter = _Waiter(req, now, deadline)
                self._commit_locked(waiter, now)
            else:
                waiter = _Waiter(req, now, deadline)
                self._queue.append(waiter)

            if waiter.granted:
                self._grant_seq += 1
                grant = Grant(self, req, now)
                self._grants[grant] = True
                return grant

        while True:
            if waiter.deadline is None:
                poll = 0.1
            else:
                poll = min(0.1, max(0.0, waiter.deadline - time.monotonic()))
            waiter.event.wait(timeout=poll)
            with self._lock:
                now = time.monotonic()
                waiter.event.clear()
                if waiter.granted:
                    self._touch_locked(now)
                    grant = Grant(self, req, time.monotonic())
                    self._grants[grant] = True
                    return grant
                if waiter.deadline is not None and now >= waiter.deadline:
                    self._touch_locked(now)
                    try:
                        self._queue.remove(waiter)
                    except ValueError:
                        pass
                    self._schedule_locked(now)
                    raise self._finish_rejection_locked(waiter, "timeout", now)

    reserve = acquire  # 预留语义：申请到即视为预留全程峰值向量

    def _return(self, grant: Grant, partial: Optional[Vector]) -> None:
        with self._lock:
            now = time.monotonic()
            self._touch_locked(now)

            if grant._released:
                return

            if partial is None:
                returned = grant._remaining
            else:
                part = partial if isinstance(partial, Vector) else Vector(partial)
                if len(part) != self._dim:
                    raise ValueError("释放向量维度不匹配")
                if not part.fits(grant._remaining):
                    raise ValueError("释放量超过当前持有量")
                returned = part

            self._usage = self._usage - returned
            grant._remaining = grant._remaining - returned

            if grant._remaining == Vector.zero(self._dim):
                grant._released = True
                self._grants.pop(grant, None)
                self._sum_hold += now - grant._granted_at
                grant._granted_at = now  # 后续重复 release 即时返回

            self._schedule_locked(now)

    def try_acquire(self, request) -> Optional[Grant]:
        try:
            return self.acquire(request, timeout=0)
        except Rejected:
            return None

    # ------------------------------------------------------------------ #
    # 观测
    # ------------------------------------------------------------------ #
    @property
    def capacity(self) -> Vector:
        return self._capacity

    @property
    def usage(self) -> Vector:
        with self._lock:
            return self._usage

    @property
    def queue_length(self) -> int:
        with self._lock:
            return len(self._queue)

    def stats(self) -> dict:
        with self._lock:
            now = time.monotonic()
            self._touch_locked(now)
            total = self._total
            admitted = self._admitted
            rejected = self._rejected
            utilization = [
                self._used_area[i] / ((self._stats_origin_elapsed(now)) * self._capacity[i])
                for i in range(self._dim)
            ]
            mean_wait = self._sum_wait / total if total else 0.0
            variance = (self._sum_wait_sq / total - mean_wait ** 2) if total else 0.0
            samples = sorted(self._wait_samples)
            def pct(p):
                if not samples:
                    return 0.0
                idx = min(len(samples) - 1, int(p * len(samples)))
                return samples[idx]
            mean_hold = self._sum_hold / admitted if admitted else 0.0
            return {
                "total": total,
                "admitted": admitted,
                "rejected": rejected,
                "rejected_over_capacity": self._rejected_over,
                "rejected_timeout": self._rejected_timeout,
                "rejection_rate": rejected / total if total else 0.0,
                "utilization": utilization,
                "mean_utilization": sum(utilization) / len(utilization),
                "wait_mean": mean_wait,
                "wait_std": max(0.0, variance) ** 0.5,
                "wait_p50": pct(0.50),
                "wait_p95": pct(0.95),
                "wait_max": self._max_wait,
                "mean_hold": mean_hold,
                "queue_length_now": len(self._queue),
            }

    def _stats_origin_elapsed(self, now: float) -> float:
        return max(1e-9, now - self._origin)
