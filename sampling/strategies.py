"""抽样策略接口与三种标准策略。

参数语义统一约定：
    sample(population, k, rng) -> list
        population : 有限总体（可迭代，内部物化为 list）
        k          : 抽样条数（int，0 <= k <= len(population)，否则 SampleSizeError）
        rng        : 注入的随机源（见 sources.py）
    所有策略均为无放回抽样；返回顺序为抽中顺序。
"""

import bisect
import math
from abc import ABC, abstractmethod

from .errors import SampleSizeError, WeightError, RandomSourceUnavailable


def _check_k(n, k):
    if not isinstance(k, int) or isinstance(k, bool):
        raise SampleSizeError("样本量 k 必须是 int，得到 %r" % (k,))
    if k < 0:
        raise SampleSizeError("样本量 k=%d 为负数" % k)
    if k > n:
        raise SampleSizeError(
            "样本量 k=%d 大于总体量 n=%d（无放回抽样）" % (k, n))


def _uniform_pick(items, k, rng):
    """无放回等概率抽取 k 条：部分 Fisher-Yates 洗牌。"""
    n = len(items)
    idx = list(range(n))
    for i in range(k):
        j = i + rng.randbelow(n - i)
        idx[i], idx[j] = idx[j], idx[i]
    return [items[t] for t in idx[:k]]


def largest_remainder(sizes, k):
    """按比例分配 k 个名额：下取整后按最大余数法补齐（同余数时序号小者优先）。"""
    n = sum(sizes)
    if n == 0:
        return [0] * len(sizes)
    raw = [k * s / n for s in sizes]
    alloc = [math.floor(x) for x in raw]
    leftover = k - sum(alloc)
    order = sorted(range(len(sizes)), key=lambda i: (-(raw[i] - alloc[i]), i))
    for i in order[:leftover]:
        alloc[i] += 1
    return alloc


class SamplingStrategy(ABC):
    """策略接口：所有策略实现统一的 sample(population, k, rng)。"""

    @abstractmethod
    def sample(self, population, k, rng):
        ...


class UniformSampling(SamplingStrategy):
    """等概率无放回抽样。"""

    def sample(self, population, k, rng):
        items = list(population)
        _check_k(len(items), k)
        return _uniform_pick(items, k, rng)


class WeightedSampling(SamplingStrategy):
    """按权重无放回抽样（权重为相对值，内部按累计分布逐个抽取并移除）。

    权重约束：长度与总体一致、非负、非 NaN/Inf、至少一个为正，
    否则抛 WeightError。权重为零的条目永远不会被抽中。
    """

    def __init__(self, weights):
        self.weights = list(weights)

    def _validate(self, weights, n):
        if len(weights) != n:
            raise WeightError(
                "权重长度 %d 与总体量 %d 不一致" % (len(weights), n))
        for w in weights:
            if not isinstance(w, (int, float)) or isinstance(w, bool) \
                    or math.isnan(w) or math.isinf(w) or w < 0:
                raise WeightError("非法权重：%r（需为非负有限实数）" % (w,))
        if not any(w > 0 for w in weights):
            raise WeightError("权重全为零，无法抽样")

    def sample(self, population, k, rng):
        items = list(population)
        _check_k(len(items), k)
        weights = list(self.weights)
        self._validate(weights, len(items))
        out = []
        for _ in range(k):
            total = sum(weights)
            if total <= 0:
                raise WeightError(
                    "剩余条目权重全为零，无法继续无放回抽取"
                    "（正权重条目数不足 k=%d）" % k)
            r = rng.random() * total
            acc = 0.0
            cum = []
            for w in weights:
                acc += w
                cum.append(acc)
            i = bisect.bisect_right(cum, r)
            if i >= len(items):
                raise RandomSourceUnavailable(
                    "随机源 random() 返回值越界（应 < 1.0）")
            out.append(items.pop(i))
            weights.pop(i)
        return out


class StratifiedSampling(SamplingStrategy):
    """分层抽样：按 key_fn 分层，层内等概率无放回，层间按比例分配（最大余数法）。"""

    def __init__(self, key_fn):
        if not callable(key_fn):
            raise TypeError("key_fn 必须可调用")
        self.key_fn = key_fn

    def sample(self, population, k, rng):
        items = list(population)
        _check_k(len(items), k)
        groups = {}
        for rec in items:
            groups.setdefault(self.key_fn(rec), []).append(rec)
        alloc = largest_remainder([len(g) for g in groups.values()], k)
        out = []
        for group, a in zip(groups.values(), alloc):
            out.extend(_uniform_pick(group, a, rng))
        return out
