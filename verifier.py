"""TOTP 校验：漂移容忍 + 自适应 + 防重放 + 常量时间比较。

常量时间说明
------------
1. 口令逐位比较使用 ``hmac.compare_digest``：CPython 在等长输入上
   按固定次数逐字节比较，不随匹配前缀长度提前返回。
2. 每次校验固定遍历全部候选窗口（back + forward + 1 个），
   即使中途匹配也不 break，避免“命中第几个窗口”造成时序差。
3. 提交值长度非法时，同样对全部候选窗口做哑比较，
   使执行路径的耗时只与候选窗口数有关，不泄露前缀信息。
4. 常量时间只覆盖“比对”阶段；重放记录/落盘由业务存储完成，
   其耗时不随口令内容变化，不能用于猜测口令。
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Callable, Optional

from drift import DriftEstimator
from replay import ReplayStore
from totp import TOTP, hotp


def const_time_equal(a: str, b: str) -> bool:
    """等长常量时间字符串比较；长度不等也不裸返回。

    长度本身是提交方已知的信息；等长输入上 compare_digest
    不泄露任何前缀匹配长度信息。
    """
    if len(a) != len(b):
        # 哑比较，保持比较操作数量与正常路径一致
        hmac.compare_digest(a.encode("ascii"), b"0" * len(a))
        return False
    return hmac.compare_digest(a.encode("ascii"), b.encode("ascii"))


class Verifier:
    def __init__(
        self,
        totp: TOTP,
        replay: ReplayStore,
        drift: Optional[DriftEstimator] = None,
        back: int = 1,
        forward: int = 0,
        learn_back: int = 4,
        learn_forward: int = 1,
        user: object = "default",
    ) -> None:
        if back < 0 or forward < 0 or learn_back < 0 or learn_forward < 0:
            raise ValueError("容忍窗口数不能为负")
        if learn_back < back or learn_forward < forward:
            raise ValueError("学习窗口必须不小于正常窗口")
        self.totp = totp
        self.replay = replay
        self.drift = drift if drift is not None else DriftEstimator()
        self.back = back
        self.forward = forward
        self.learn_back = learn_back
        self.learn_forward = learn_forward
        self.user = user

    def _candidate_range(self, current: int) -> tuple[int, int]:
        """根据漂移估计/学习期给出候选窗口区间。"""
        if self.drift.is_learning(self.user):
            return current - self.learn_back, current + self.learn_forward
        est = self.drift.estimate(self.user)
        return current + est - self.back, current + est + self.forward

    def verify(self, code: str, at_time: Optional[float] = None) -> bool:
        """校验提交口令；成功则记录重放与漂移样本。

        时间可注入（at_time），否则使用 TOTP 自带时钟。
        """
        if at_time is None:
            at_time = self.totp.clock()
        current = self.totp.counter_at(at_time)
        lo, hi = self._candidate_range(current)

        # 格式校验不提前返回：非法格式走哑比较，保持耗时稳定
        valid_format = (
            isinstance(code, str)
            and len(code) == self.totp.digits
            and code.isdigit()
        )
        probe = code if valid_format else "0" * self.totp.digits

        matched_counter = None
        for counter in range(lo, hi + 1):  # 固定遍历全部窗口，不提前退出
            candidate = hotp(
                self.totp.key, counter, self.totp.digits, self.totp.digest
            )
            if const_time_equal(candidate, probe) and valid_format:
                # 命中也不 break；若多个窗口算出同值取最接近当前的一个
                if matched_counter is None or abs(counter - current) < abs(
                    matched_counter - current
                ):
                    matched_counter = counter

        if matched_counter is None:
            return False
        if self.replay.seen(self.user, matched_counter):
            return False
        self.replay.record(self.user, matched_counter, current)
        self.drift.record(self.user, matched_counter - current)
        return True
