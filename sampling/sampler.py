"""Sampler：把策略与随机源绑定为可复用的抽样组件。"""

from .sources import require_source


class Sampler:
    def __init__(self, strategy, rng):
        self._strategy = strategy
        self._rng = require_source(rng)

    def sample(self, population, k):
        return self._strategy.sample(population, k, self._rng)
