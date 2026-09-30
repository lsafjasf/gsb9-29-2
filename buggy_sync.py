"""原始（有缺陷）的同步实现：纯滑动平均。

缺陷（由 test_reproduce_buggy.py 稳定复现）：
1. 无延迟筛选：所有测量等权平均，非对称抖动的均值直接变成固定偏差；
2. 无频偏估计：只估计瞬时偏移 offset，不估计频偏 skew，晶振频偏使
   滑动平均的滞后随时间持续累积（长跑越漂越大）；
3. 无跳变检测：时钟阶跃被滑动窗口当成普通测量，用一个窗口的时间
   慢慢"吃掉"，期间无任何事件、无重新收敛；
4. 无异常过滤：单个网络尖峰直接污染整个窗口的均值。
"""
from __future__ import annotations

from collections import deque


class BuggySynchronizer:
    def __init__(self, window: int = 8):
        self.window = window
        self.offsets: deque[float] = deque(maxlen=window)
        self.events: list[dict] = []  # 永远为空：原始实现不记录任何事件

    def update(self, t1: float, t2: float, t3: float, t4: float) -> None:
        offset = ((t2 - t1) + (t3 - t4)) / 2.0
        self.offsets.append(offset)
        return None

    def estimate(self, t: float | None = None) -> float | None:
        if not self.offsets:
            return None
        return sum(self.offsets) / len(self.offsets)
