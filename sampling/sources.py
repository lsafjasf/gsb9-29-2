"""随机源抽象与适配器。

所有策略只依赖一个鸭子类型接口：

    random() -> float        # 返回 [0.0, 1.0) 均匀随机数
    randbelow(n) -> int      # 返回 [0, n) 均匀随机整数

随机源一律由调用方注入，组件内部不自行创建，
以保证同一随机源下各模块结果可复现、可对齐。
"""

import random

from .errors import RandomSourceUnavailable


class SeededRandom:
    """可复现随机源，包装 random.Random。"""

    def __init__(self, seed):
        self._r = random.Random(seed)

    def random(self):
        return self._r.random()

    def randbelow(self, n):
        return self._r.randrange(n)


class SystemRandomSource:
    """系统熵源（不可复现），包装 random.SystemRandom。"""

    def __init__(self):
        self._r = random.SystemRandom()

    def random(self):
        return self._r.random()

    def randbelow(self, n):
        return self._r.randrange(n)


class FuncSource:
    """适配遗留调用点：只提供一个 () -> [0,1) 浮点函数的场合。

    randbelow 的推导方式与遗留代码一致：int(f() * n)。
    """

    def __init__(self, func):
        if not callable(func):
            raise RandomSourceUnavailable("FuncSource 需要一个可调用对象")
        self._f = func

    def random(self):
        v = self._f()
        if not (0.0 <= v < 1.0):
            raise RandomSourceUnavailable(
                "随机源函数返回值越界：%r 不在 [0.0, 1.0)" % (v,))
        return v

    def randbelow(self, n):
        v = int(self._f() * n)
        if not (0 <= v < n):
            raise RandomSourceUnavailable(
                "随机源函数返回值越界，无法映射到 [0, %d)" % n)
        return v


def require_source(rng):
    """校验注入的随机源接口完整，否则抛 RandomSourceUnavailable。"""
    if rng is None:
        raise RandomSourceUnavailable(
            "未注入随机源：请传入 SeededRandom / SystemRandomSource / FuncSource")
    if not callable(getattr(rng, "random", None)) \
            or not callable(getattr(rng, "randbelow", None)):
        raise RandomSourceUnavailable(
            "随机源接口不完整：需要 random() 与 randbelow(n) 两个方法")
    return rng
