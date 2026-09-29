"""ETL 导出模块 —— 重构前的原始实现。"""

import bisect
import random


def sample_records(records, weights, k, seed=None):
    """按权重抽 k 条导出。seed=None 时每次新建 Random，结果不可复现。"""
    rng = random.Random(seed)
    weights = [w if w > 0 else 0.0 for w in weights]  # 负权重被静默清零
    items = list(records)
    out = []
    for _ in range(k):
        total = sum(weights)
        r = rng.random() * total
        acc = 0.0
        cum = []
        for w in weights:
            acc += w
            cum.append(acc)
        i = bisect.bisect_right(cum, r)
        out.append(items.pop(i))
        weights.pop(i)
    return out
