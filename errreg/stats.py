"""Statistical helpers for error-regression testing (standard library only).

The framework must tolerate environment noise (cross-platform floating
point differences, randomized algorithm fluctuation) without raising
false alarms, so all comparisons are expressed as statistical tests
with explicit false-positive rates instead of ad-hoc fudge factors.
"""

from __future__ import annotations

import math

# One-sided Student-t critical values, tabulated for the tail
# probabilities the framework uses.  Values for df not listed are
# interpolated in 1/df, which is accurate to <0.5% for these tables.
_T_TABLE = {
    0.05: {
        1: 6.314, 2: 2.920, 3: 2.353, 4: 2.132, 5: 2.015,
        6: 1.943, 7: 1.895, 8: 1.860, 9: 1.833, 10: 1.812,
        12: 1.782, 15: 1.753, 20: 1.725, 25: 1.708, 30: 1.697,
        40: 1.684, 60: 1.671, 120: 1.658, math.inf: 1.645,
    },
    0.01: {
        1: 31.821, 2: 6.965, 3: 4.541, 4: 3.747, 5: 3.365,
        6: 3.143, 7: 2.998, 8: 2.896, 9: 2.821, 10: 2.764,
        12: 2.681, 15: 2.602, 20: 2.528, 25: 2.485, 30: 2.457,
        40: 2.423, 60: 2.390, 120: 2.358, math.inf: 2.326,
    },
    0.005: {
        1: 63.657, 2: 9.925, 3: 5.841, 4: 4.604, 5: 4.032,
        6: 3.707, 7: 3.499, 8: 3.355, 9: 3.250, 10: 3.169,
        12: 3.055, 15: 2.947, 20: 2.845, 25: 2.787, 30: 2.750,
        40: 2.704, 60: 2.660, 120: 2.617, math.inf: 2.576,
    },
}


def t_critical(df: float, alpha: float = 0.01) -> float:
    """One-sided Student-t critical value t_{1-alpha, df}.

    Interpolates in 1/df between tabulated points.  ``df`` is clamped
    to >= 1 so that a baseline built from a single sample yields the
    most conservative (widest) interval instead of crashing.
    """
    if alpha not in _T_TABLE:
        alpha = min(_T_TABLE, key=lambda a: abs(a - alpha))
    table = _T_TABLE[alpha]
    df = max(1.0, float(df))
    if df in table:
        return table[df]
    points = sorted(table)
    lo = max((d for d in points if d <= df), default=points[0])
    hi = min((d for d in points if d >= df), default=points[-1])
    if lo == hi:
        return table[lo]
    # Linear interpolation in 1/df (exact at both ends, df=inf -> 0).
    x, x0, x1 = 1.0 / df, 1.0 / lo, 1.0 / hi
    w = (x - x0) / (x1 - x0)
    return table[lo] + w * (table[hi] - table[lo])


def welch_df(var0: float, n0: int, var1: float, n1: int) -> float:
    """Welch-Satterthwaite degrees of freedom for a two-sample comparison."""
    a = var0 / n0 if n0 > 0 else 0.0
    b = var1 / n1 if n1 > 0 else 0.0
    total = a + b
    if total <= 0.0:
        return max(1.0, (n0 + n1 - 2) if (n0 + n1) > 2 else 1.0)
    num = total * total
    den = 0.0
    if n0 > 1:
        den += a * a / (n0 - 1)
    if n1 > 1:
        den += b * b / (n1 - 1)
    if den <= 0.0:
        return 1.0
    return max(1.0, num / den)


def summarize(samples):
    """Return (mean, sample_std, n, min, max) for a sequence of floats."""
    xs = list(samples)
    n = len(xs)
    if n == 0:
        raise ValueError("cannot summarize an empty sample set")
    mean = math.fsum(xs) / n
    if n > 1:
        var = math.fsum((x - mean) ** 2 for x in xs) / (n - 1)
    else:
        var = 0.0
    return mean, math.sqrt(var), n, min(xs), max(xs)
