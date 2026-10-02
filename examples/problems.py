"""Demo numerical problems with high-precision (Decimal) reference
solutions and deliberately degraded variants used to exercise the
error-regression framework.
"""

from __future__ import annotations

import math
import random
from decimal import Decimal, localcontext

from errreg.runner import Case


# --------------------------------------------- high-precision references

def sqrt2_reference() -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 60
        return Decimal(2).sqrt()


def _arccot(x: int) -> Decimal:
    # arctan(1/x) = 1/x - 1/(3 x^3) + 1/(5 x^5) - ...
    xd = Decimal(x)
    total = term = Decimal(1) / xd
    x2 = xd * xd
    n = 1
    while n < 100000:
        term /= x2
        delta = term / Decimal(2 * n + 1)
        if delta == 0:
            break
        if n % 2:
            total -= delta
        else:
            total += delta
        n += 1
    return total


def pi_reference() -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 70
        # Machin's formula: pi = 16 arctan(1/5) - 4 arctan(1/239)
        value = 16 * _arccot(5) - 4 * _arccot(239)
        return +value


# ------------------------------------------------ deterministic: Newton sqrt

def make_sqrt2_newton(tol: float = 1e-16, ulp_jitter: int = 0,
                      extra_iterations: int = 0, max_iter: int = 100):
    """Newton iteration for sqrt(2).

    ``ulp_jitter`` nudges the input by +/- that many ulps before
    solving, which models cross-platform input/rounding differences
    while leaving the reference at sqrt(2) exactly.
    """

    def trial(rng: random.Random):
        x = 2.0
        for _ in range(ulp_jitter):
            toward = 0.0 if rng.random() < 0.5 else 4.0
            x = math.nextafter(x, toward)
        guess = 1.0
        iters = 0
        while iters < max_iter:
            nxt = 0.5 * (guess + x / guess)
            iters += 1
            if abs(nxt - guess) <= tol * x:
                guess = nxt
                break
            guess = nxt
        for _ in range(extra_iterations):
            guess = 0.5 * (guess + x / guess)
            iters += 1
        return guess, iters

    return trial


# ------------------------------------------------ randomized: Monte Carlo pi

MC_SAMPLES = 20000


def make_pi_monte_carlo(hi: float = 1.0):
    """Estimate pi = 4 * integral_0^1 1/(1+x^2) dx.

    ``hi < 1`` is a subtle truncation bias (the sampled interval is
    silently clipped) that improves variance slightly but shifts the
    mean: the sort of regression functional tests miss.
    """

    def trial(rng: random.Random):
        total = 0.0
        for _ in range(MC_SAMPLES):
            u = hi * rng.random()
            total += 1.0 / (1.0 + u * u)
        return 4.0 * total / MC_SAMPLES, MC_SAMPLES

    return trial


# ----------------------------------------------------------------- cases

def good_cases():
    """Cases used to build baselines (all should PASS)."""
    return [
        Case(
            name="sqrt2_newton",
            kind="deterministic",
            reference_fn=sqrt2_reference,
            trial_fn=make_sqrt2_newton(ulp_jitter=3),
            baseline_trials=12,
            check_trials=5,
            meta={"algorithm": "Newton sqrt", "noise": "+/-3 ulp input jitter"},
        ),
        Case(
            name="sqrt2_newton_exact",
            kind="deterministic",
            reference_fn=sqrt2_reference,
            trial_fn=make_sqrt2_newton(ulp_jitter=0),
            baseline_trials=6,
            check_trials=3,
            meta={"algorithm": "Newton sqrt", "noise": "none (bit-identical)"},
        ),
        Case(
            name="sqrt2_newton_sparse",
            kind="deterministic",
            reference_fn=sqrt2_reference,
            trial_fn=make_sqrt2_newton(ulp_jitter=3),
            baseline_trials=2,
            check_trials=3,
            meta={"algorithm": "Newton sqrt", "noise": "baseline built from 2 runs"},
        ),
        Case(
            name="pi_monte_carlo",
            kind="randomized",
            reference_fn=pi_reference,
            trial_fn=make_pi_monte_carlo(),
            baseline_trials=30,
            check_trials=30,
            meta={"algorithm": "Monte Carlo", "samples_per_trial": MC_SAMPLES},
        ),
    ]


def degraded_cases():
    """Modified versions of the same problems; they must FAIL.
    They check against the baseline of the unmodified algorithm."""
    return [
        Case(
            name="sqrt2_newton_early_stop",
            kind="deterministic",
            baseline_name="sqrt2_newton",
            reference_fn=sqrt2_reference,
            # iteration budget silently cut: error ~2e-6, no crash,
            # functional tests on the value's first digits still pass
            trial_fn=make_sqrt2_newton(max_iter=3, ulp_jitter=3),
            check_trials=5,
        ),
        Case(
            name="sqrt2_newton_extra_iters",
            kind="deterministic",
            baseline_name="sqrt2_newton",
            reference_fn=sqrt2_reference,
            # same accuracy, but 8 wasted iterations per solve
            trial_fn=make_sqrt2_newton(ulp_jitter=3, extra_iterations=8),
            check_trials=5,
        ),
        Case(
            name="pi_monte_carlo_biased",
            kind="randomized",
            baseline_name="pi_monte_carlo",
            reference_fn=pi_reference,
            # clipped sampling interval: small but real bias (~0.7%);
            # calibrated so 30 trials give the test sufficient power
            trial_fn=make_pi_monte_carlo(hi=0.98),
            check_trials=30,
        ),
    ]
