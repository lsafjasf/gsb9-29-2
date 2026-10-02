"""Statistical primitives for noise-tolerant error regression checks.

Standard library only.  The central idea: a baseline is a *distribution*
of historical error samples, not a single number.  A fresh measurement is
compared against a one-sided 99.5% prediction/two-sample bound built with
a Student-t factor, so

* cross-platform float noise and randomized-algorithm fluctuation are
  absorbed by the interval width, and
* tiny-sample baselines automatically get much wider intervals (t with
  few degrees of freedom), which keeps the false-positive rate controlled
  instead of pretending we know the variance.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Sequence

# Student-t critical values, two-sided 99% (== one-sided 99.5%), df 1..30.
_T_995 = {
    1: 63.657, 2: 9.925, 3: 5.841, 4: 4.604, 5: 4.032,
    6: 3.707, 7: 3.499, 8: 3.355, 9: 3.250, 10: 3.169,
    11: 3.106, 12: 3.055, 13: 3.012, 14: 2.977, 15: 2.947,
    16: 2.921, 17: 2.898, 18: 2.878, 19: 2.861, 20: 2.845,
    21: 2.831, 22: 2.819, 23: 2.807, 24: 2.797, 25: 2.787,
    26: 2.779, 27: 2.771, 28: 2.763, 29: 2.756, 30: 2.750,
}

_Z_995 = 2.5758293035489004  # normal quantile, one-sided 99.5%


def t_critical(df: int) -> float:
    """One-sided 99.5% Student-t critical value for ``df`` degrees of freedom."""
    if df < 1:
        raise ValueError("degrees of freedom must be >= 1")
    if df <= 30:
        return _T_995[df]
    # Cornish-Fisher expansion around the normal quantile for large df.
    z = _Z_995
    g1 = (z ** 3 + z) / 4.0
    g2 = (5 * z ** 5 + 16 * z ** 3 + 3 * z) / 96.0
    return z + g1 / df + g2 / df ** 2


@dataclass
class Summary:
    n: int
    mean: float
    stdev: float
    minimum: float
    maximum: float

    def as_dict(self) -> dict:
        return {
            "n": self.n,
            "mean": self.mean,
            "stdev": self.stdev,
            "min": self.minimum,
            "max": self.maximum,
        }


def summarize(samples: Sequence[float]) -> Summary:
    if not samples:
        raise ValueError("need at least one sample")
    n = len(samples)
    mean = statistics.fmean(samples)
    stdev = statistics.stdev(samples) if n >= 2 else 0.0
    return Summary(n=n, mean=mean, stdev=stdev,
                   minimum=min(samples), maximum=max(samples))


def degradation_limit(base: Summary, current_n: int) -> float:
    """Upper bound for the current mean error before it counts as a regression.

    Two-sample style bound::

        base.mean + t_(n-1) * s * sqrt(1/n + 1/m)

    With a single current sample (m = 1) this reduces to the classic
    prediction interval for one new observation.  The one-sided level is
    99.5%, so a stable algorithm trips the alarm only ~0.5% of the time
    per case under the normal model.
    """
    if base.n < 2 or base.stdev == 0.0:
        raise ValueError("degradation_limit requires a noisy baseline (n>=2, stdev>0)")
    if current_n < 1:
        raise ValueError("current_n must be >= 1")
    t = t_critical(base.n - 1)
    se = base.stdev * math.sqrt(1.0 / base.n + 1.0 / current_n)
    return base.mean + t * se
