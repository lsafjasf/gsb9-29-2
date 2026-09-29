"""无偏性验证用的统计工具（仅标准库）。"""

import math


def chi2_statistic(observed, expected):
    """Pearson 卡方统计量。expected 中为零的项要求 observed 也为零。"""
    stat = 0.0
    for o, e in zip(observed, expected):
        if e == 0:
            if o != 0:
                return math.inf
            continue
        stat += (o - e) ** 2 / e
    return stat


def _gammq(a, x):
    """正则化上不完全伽马函数 Q(a, x)（Numerical Recipes 的级数/连分式实现）。"""
    if x < 0 or a <= 0:
        raise ValueError("gammq: 需要 a > 0 且 x >= 0")
    if x == 0:
        return 1.0
    gln = math.lgamma(a)
    if x < a + 1.0:
        ap = a
        term = 1.0 / a
        total = term
        for _ in range(1000):
            ap += 1.0
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return 1.0 - total * math.exp(-x + a * math.log(x) - gln)
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return h * math.exp(-x + a * math.log(x) - gln)


def chi2_sf(stat, df):
    """卡方分布的生存函数（p 值）：P(Chi2_df >= stat)。"""
    if df <= 0:
        raise ValueError("自由度必须为正")
    return _gammq(df / 2.0, stat / 2.0)
