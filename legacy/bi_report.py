"""BI 报表模块 —— 重构前的原始实现。"""

import random


def sample_rows(rows, k):
    """从报表行中随机抽 k 行。直接用全局 random，无法复现。"""
    idx = list(range(len(rows)))
    for i in range(k):
        j = i + random.randrange(len(rows) - i)
        idx[i], idx[j] = idx[j], idx[i]
    return [rows[t] for t in idx[:k]]
