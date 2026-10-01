"""客户端时钟漂移估计：按用户历史成功窗口自适应调整校验容忍中心。

原理
----
每次校验成功时记录 offset = 匹配窗口 - 服务器当前窗口。
该 offset 正是客户端时钟相对服务器时钟的漂移（以窗口为单位）。
对最近若干次样本取中位数并四舍五入，得到漂移估计 est；
校验窗口由固定的 [cur-back, cur+fwd] 平移为
[cur+est-back, cur+est+fwd]，从而跟随客户端时钟。

学习期
------
样本不足 min_samples 时无法可靠估计，校验器改用更宽的
学习窗口（learn_back/learn_forward）以收集样本；
样本足够后收紧到估计值附近的窄窗口，降低被暴力枚举的表面积。
"""
from __future__ import annotations

import statistics
from collections import deque
from typing import Deque, Dict, Hashable


class DriftEstimator:
    def __init__(self, max_samples: int = 16, min_samples: int = 3) -> None:
        if min_samples < 1 or max_samples < min_samples:
            raise ValueError("需要 1 <= min_samples <= max_samples")
        self.max_samples = max_samples
        self.min_samples = min_samples
        self._samples: Dict[Hashable, Deque[int]] = {}

    def record(self, user: Hashable, offset: int) -> None:
        """记录一次成功校验的窗口偏移。"""
        buf = self._samples.setdefault(user, deque(maxlen=self.max_samples))
        buf.append(int(offset))

    def sample_count(self, user: Hashable) -> int:
        buf = self._samples.get(user)
        return len(buf) if buf else 0

    def samples(self, user: Hashable) -> list[int]:
        buf = self._samples.get(user)
        return list(buf) if buf else []

    def is_learning(self, user: Hashable) -> bool:
        return self.sample_count(user) < self.min_samples

    def estimate(self, user: Hashable) -> int:
        """漂移估计（窗口数）。无样本时返回 0（假设时钟同步）。"""
        buf = self._samples.get(user)
        if not buf:
            return 0
        # 中位数对偶发抖动/异常值稳健；round 为银行家舍入，
        # 由于容忍窗口本身有 ±back/fwd 余量，半窗口误差可被吸收。
        return round(statistics.median(buf))
