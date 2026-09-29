"""目的地与隔离投递 worker。

每个目的地独占一个有界队列 + 一个消费线程：
- 队列满时按策略丢弃（drop_newest：丢弃新记录），并计数；
- 某目的地阻塞/失败只影响自己的队列，不影响其他目的地；
- 缓冲区上界 = queue_size，全系统上界 = 各目的地 queue_size 之和。
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Mapping


class Destination:
    """目的地接口：send 投递一条记录，抛异常视为该条失败。"""

    name = "destination"

    def send(self, record: Mapping[str, Any]) -> None:  # pragma: no cover
        raise NotImplementedError


class LatencyStats:
    """延迟统计：精确 count/min/max/sum + 有界样本计算分位数。"""

    def __init__(self, sample_size: int = 1024):
        self.count = 0
        self.total = 0.0
        self.min = None
        self.max = None
        self._samples = []
        self._sample_size = sample_size

    def observe(self, seconds: float) -> None:
        self.count += 1
        self.total += seconds
        self.min = seconds if self.min is None else min(self.min, seconds)
        self.max = seconds if self.max is None else max(self.max, seconds)
        if len(self._samples) < self._sample_size:
            self._samples.append(seconds)
        else:
            # 简单蓄水池替换，保持样本有界
            self._samples[self.count % self._sample_size] = seconds

    def snapshot(self) -> dict:
        samples = sorted(self._samples)
        p95 = samples[int(len(samples) * 0.95)] if samples else None
        return {
            "count": self.count,
            "avg_ms": (self.total / self.count * 1000.0) if self.count else 0.0,
            "min_ms": (self.min * 1000.0) if self.min is not None else None,
            "max_ms": (self.max * 1000.0) if self.max is not None else None,
            "p95_ms": (p95 * 1000.0) if p95 is not None else None,
        }


class DestinationWorker:
    """单个目的地的隔离投递单元。"""

    DROP_NEWEST = "drop_newest"

    def __init__(self, destination: Destination, queue_size: int = 1024,
                 drop_policy: str = DROP_NEWEST):
        if drop_policy != self.DROP_NEWEST:
            raise ValueError("unsupported drop policy: %r" % drop_policy)
        self.destination = destination
        self.name = destination.name
        self.queue_size = queue_size
        self.drop_policy = drop_policy
        self._queue: "queue.Queue" = queue.Queue(maxsize=queue_size)
        self._lock = threading.Lock()
        self.delivered = 0
        self.dropped = 0       # 队列满被丢弃
        self.failed = 0        # send 抛异常
        self.latency = LatencyStats()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="logrouter-%s" % self.name, daemon=True)
        self._thread.start()

    def enqueue(self, record: Mapping[str, Any]) -> bool:
        """非阻塞入队；队列满则按策略丢弃并计数。返回是否入队成功。"""
        try:
            self._queue.put_nowait(record)
            return True
        except queue.Full:
            with self._lock:
                self.dropped += 1
            return False

    def _run(self) -> None:
        while True:
            record = self._queue.get()
            if record is _STOP:
                self._queue.task_done()
                return
            started = time.monotonic()
            try:
                self.destination.send(record)
            except Exception:
                with self._lock:
                    self.failed += 1
            else:
                with self._lock:
                    self.delivered += 1
                self.latency.observe(time.monotonic() - started)
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 5.0) -> bool:
        """等待队列排空（测试/收尾用）。"""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.005)
        return True

    def close(self, timeout: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        # 队列可能已满且 worker 卡死，投递停止信号不能无限阻塞
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._queue.put_nowait(_STOP)
                break
            except queue.Full:
                if time.monotonic() > deadline:
                    break
                time.sleep(0.01)
        self._thread.join(timeout)

    def stats(self) -> dict:
        with self._lock:
            return {
                "delivered": self.delivered,
                "dropped": self.dropped,
                "failed": self.failed,
                "queue_size": self.queue_size,
                "drop_policy": self.drop_policy,
                "latency": self.latency.snapshot(),
            }


class _Stop:
    pass


_STOP = _Stop()
