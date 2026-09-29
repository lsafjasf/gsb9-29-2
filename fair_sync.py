"""公平信号量与屏障（仅使用 Python 标准库）。

设计要点
--------
FairSemaphore
  * 严格 FIFO：等待者进入 deque 队尾，许可只授予队首；队列非空时新到线程
    必须排队，不允许插队（barging-free）。
  * 超时安全：超时路径在持锁状态下检查 granted 标志，许可不会丢失，
    也不会被重复授予。
  * 释放校验：release(n) 要求 1 <= n <= 当前持有数，否则抛 ValueError。
  * 不变量：held + available == capacity 且 0 <= held <= capacity，
    且"队列非空 => available == 0"，随时可用 check_invariants() 断言。

FairBarrier
  * 代（generation）机制：每代只在集齐 parties 个线程时放行一次，
    绝不重复放行（每放行一次 generation 恰好 +1）。
  * 超时：任一等待者超时会使屏障进入 broken 状态，同代其他等待者收到
    threading.BrokenBarrierError；reset() 后屏障恢复可用。
  * parties == 0 时 wait() 立即返回（空集合 vacuously 同步）。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager

__all__ = [
    "FairSemaphore",
    "FairBarrier",
    "BarrierTimeoutError",
    "BrokenBarrierError",
]

BrokenBarrierError = threading.BrokenBarrierError


class BarrierTimeoutError(TimeoutError):
    """屏障等待超时（是内置 TimeoutError 的子类）。"""


class _Waiter:
    __slots__ = ("granted",)

    def __init__(self) -> None:
        self.granted = False


class FairSemaphore:
    """严格 FIFO 的计数信号量，支持超时获取与释放计数校验。"""

    def __init__(self, capacity: int = 1):
        if not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be an integer >= 1")
        self._capacity = capacity
        self._available = capacity
        self._held = 0
        self._waiters: deque[_Waiter] = deque()
        self._cond = threading.Condition()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def held(self) -> int:
        """当前被持有的许可数。"""
        with self._cond:
            return self._held

    @property
    def available(self) -> int:
        """当前空闲的许可数。"""
        with self._cond:
            return self._available

    @property
    def waiting(self) -> int:
        """当前排队中的等待者数量。"""
        with self._cond:
            return len(self._waiters)

    def check_invariants(self) -> None:
        """断言信号量不变量，任何时候都可安全调用。"""
        with self._cond:
            assert 0 <= self._held <= self._capacity, "held 越界"
            assert self._available >= 0, "available 为负"
            assert self._held + self._available == self._capacity, "许可不守恒"
            assert not (self._waiters and self._available > 0), (
                "队列非空时不得有空闲许可（否则意味着插队或丢失唤醒）"
            )

    def acquire(self, timeout: float | None = None) -> bool:
        """获取一个许可；成功返回 True，超时返回 False。

        timeout 为 None 表示无限等待；timeout 必须 >= 0。
        """
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be >= 0 or None")
        with self._cond:
            # 快速路径：无等待者且有空闲许可，直接获取（队列非空则必须排队，
            # 空闲许可是为队首保留的，新到线程不得插队）。
            if not self._waiters and self._available > 0:
                self._available -= 1
                self._held += 1
                return True

            waiter = _Waiter()
            self._waiters.append(waiter)
            deadline = None if timeout is None else time.monotonic() + timeout
            while not waiter.granted:
                if deadline is None:
                    self._cond.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # 仍持有锁：granted 不可能在此期间并发变化。
                    # 若未被授予，把自己移出队列即可，许可账目不受影响。
                    self._waiters.remove(waiter)
                    return False
                self._cond.wait(remaining)
            return True

    def release(self, n: int = 1) -> None:
        """释放 n 个许可；n 必须满足 1 <= n <= 当前持有数。"""
        if not isinstance(n, int) or n < 1:
            raise ValueError("n must be an integer >= 1")
        with self._cond:
            if n > self._held:
                raise ValueError(
                    f"cannot release {n} permit(s): only {self._held} held"
                )
            self._held -= n
            self._available += n
            self._grant_waiters()

    def _grant_waiters(self) -> None:
        """把空闲许可按 FIFO 顺序授予队首等待者。调用方须持有锁。"""
        granted = 0
        while self._waiters and self._available > 0:
            waiter = self._waiters.popleft()
            waiter.granted = True
            self._available -= 1
            self._held += 1
            granted += 1
        if granted:
            self._cond.notify_all()

    @contextmanager
    def hold(self, timeout: float | None = None):
        """上下文管理器：获取许可，退出时（含异常路径）保证释放。"""
        if not self.acquire(timeout):
            raise TimeoutError("FairSemaphore.acquire timed out")
        try:
            yield self
        finally:
            self.release()


class FairBarrier:
    """可复用的屏障：集齐 parties 个线程后同代全部放行，每代只放行一次。"""

    def __init__(self, parties: int):
        if not isinstance(parties, int) or parties < 0:
            raise ValueError("parties must be an integer >= 0")
        self._parties = parties
        self._count = 0
        self._generation = 0
        self._broken = False
        self._broken_gens: set[int] = set()
        self._cond = threading.Condition()

    @property
    def parties(self) -> int:
        return self._parties

    @property
    def n_waiting(self) -> int:
        with self._cond:
            return self._count

    @property
    def generation(self) -> int:
        """已完成的代数（放行次数 + reset 次数），可用于断言不重复放行。"""
        with self._cond:
            return self._generation

    @property
    def broken(self) -> bool:
        with self._cond:
            return self._broken

    def check_invariants(self) -> None:
        with self._cond:
            assert self._generation >= 0
            if self._parties > 0:
                assert 0 <= self._count < self._parties, "到达计数越界"
            else:
                assert self._count == 0

    def wait(self, timeout: float | None = None) -> int:
        """等待本代集齐。返回本线程的到达序号（0..parties-1）。

        超时抛 BarrierTimeoutError 并使屏障进入 broken 状态；
        屏障已 broken（或被同代等待者超时打断、或被 reset）时抛
        BrokenBarrierError。parties == 0 时立即返回 0。
        """
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be >= 0 or None")
        with self._cond:
            if self._broken:
                raise BrokenBarrierError("barrier is broken")
            if self._parties == 0:
                return 0

            gen = self._generation
            index = self._count
            self._count += 1
            if self._count == self._parties:
                # 集齐：本代放行恰好一次
                self._count = 0
                self._generation += 1
                self._cond.notify_all()
                return index

            deadline = None if timeout is None else time.monotonic() + timeout
            while (
                gen == self._generation
                and not self._broken
                and gen not in self._broken_gens
            ):
                if deadline is None:
                    self._cond.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # 本线程超时：打断屏障，同代其他等待者将收到
                    # BrokenBarrierError，直到 reset() 为止。
                    self._broken = True
                    self._count = 0
                    self._cond.notify_all()
                    raise BarrierTimeoutError("barrier wait timed out")
                self._cond.wait(remaining)

            if self._broken or gen in self._broken_gens:
                raise BrokenBarrierError("barrier was broken or reset")
            return index

    def reset(self) -> None:
        """重置屏障：当前等待者收到 BrokenBarrierError，之后可正常使用。"""
        with self._cond:
            if self._count > 0:
                self._broken_gens.add(self._generation)
            self._broken = False
            self._count = 0
            self._generation += 1
            self._cond.notify_all()
