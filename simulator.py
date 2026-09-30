"""可注入时钟与网络仿真（仅标准库）。

仿真时间由外部驱动，同步实现不直接读取系统时钟，因此测试完全确定、可复现。
"""
from __future__ import annotations

import random


class SimClock:
    """注入式时钟。

    read(t) = t * (1 + skew) + offset + 累计跳变
    skew 由 ppm 给出（如 3000ppm = 3ms/s 的频偏，模拟劣质晶振长跑漂移）。
    """

    def __init__(self, skew_ppm: float = 0.0, offset: float = 0.0):
        self.skew = skew_ppm * 1e-6
        self.offset = offset
        self._jump = 0.0

    def read(self, t: float) -> float:
        return t * (1.0 + self.skew) + self.offset + self._jump

    def step(self, delta: float) -> None:
        """时钟阶跃（如管理员校时、NTP/GPS 失锁后重捕导致的跳变）。"""
        self._jump += delta


class Link:
    """非对称、带抖动与偶发尖峰的网络链路。

    单程延迟 = base + U(0, jitter)，并以 spike_prob 的概率叠加 U(0, spike_mag)
    的突发尖峰（拥塞 / 排队 / GC 暂停）。
    """

    def __init__(
        self,
        rng: random.Random,
        up: float = 0.0,
        down: float = 0.0,
        jitter: float = 0.0,
        up_jitter: float | None = None,
        down_jitter: float | None = None,
        spike_prob: float = 0.0,
        spike_mag: float = 0.0,
    ):
        self.rng = rng
        self.up = up
        self.down = down
        self.up_jitter = jitter if up_jitter is None else up_jitter
        self.down_jitter = jitter if down_jitter is None else down_jitter
        self.spike_prob = spike_prob
        self.spike_mag = spike_mag

    def _one_way(self, base: float, jitter: float) -> float:
        delay = base + self.rng.random() * jitter
        if self.spike_prob and self.rng.random() < self.spike_prob:
            delay += self.rng.random() * self.spike_mag
        return delay

    def exchange(self, t: float, local: SimClock, remote: SimClock,
                 proc: float = 0.0) -> tuple[float, float, float, float]:
        """一次 NTP 式四次握手，返回 (t1, t2, t3, t4)。"""
        up = self._one_way(self.up, self.up_jitter)
        down = self._one_way(self.down, self.down_jitter)
        t1 = local.read(t)
        t2 = remote.read(t + up)
        t3 = remote.read(t + up + proc)
        t4 = local.read(t + up + proc + down)
        return t1, t2, t3, t4


def true_offset(remote: SimClock, local: SimClock, t: float) -> float:
    """t 时刻远端相对本地的真实偏移。"""
    return remote.read(t) - local.read(t)


def run_scenario(sync, local: SimClock, remote: SimClock, link: Link, *,
                 duration: float, interval: float, steps: tuple = (),
                 proc: float = 0.0) -> list[dict]:
    """按固定间隔驱动同步器跑完整个场景，返回逐条记录。"""
    steps = sorted(steps)
    step_idx = 0
    records = []
    n = int(duration / interval) + 1
    for k in range(n):
        t = k * interval
        while step_idx < len(steps) and steps[step_idx][0] <= t:
            remote.step(steps[step_idx][1])
            step_idx += 1
        t1, t2, t3, t4 = link.exchange(t, local, remote, proc)
        info = sync.update(t1, t2, t3, t4)
        est = sync.estimate(t4)
        truth = true_offset(remote, local, t)
        records.append({
            "t": t,
            "true_offset": truth,
            "estimate": est,
            "residual": None if est is None else est - truth,
            "info": info,
        })
    return records
