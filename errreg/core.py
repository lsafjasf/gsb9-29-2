"""Core data model and statistical comparison for error-regression tests.

Terminology
-----------
* reference   -- high-precision solution (decimal.Decimal), recomputed
                 at check time and verified against the baseline hash.
* baseline    -- historical error/iteration/runtime distribution stored
                 as JSON, one file per problem case.
* check       -- comparison of a fresh run against the baseline.

Two comparison strategies are provided:

  reproducible.  A fresh error e fails when
      e > mean0*(1+rel_tol) + abs_tol + k * sigma_eff
  where sigma_eff is floored by a platform noise floor and k is a
  Student-t *prediction-interval* multiplier
  (t_{1-alpha, n0-1} * sqrt(1 + 1/n0)) that inflates automatically
  when the baseline was built from very few samples.

* randomized -- the algorithm's error fluctuates by design (Monte
  Carlo, stochastic iteration).  Degradation is a one-sided Welch
  test against a non-inferiority margin delta: the run fails only
  when the lower (1-alpha) confidence bound on (mean1 - mean0)
  exceeds delta.  Under the null hypothesis of no change the false
  positive rate is therefore <= alpha (see reports/false_positive_data.json
  for measured values).
"""

from __future__ import annotations

import hashlib
import math
import sys
from dataclasses import dataclass, field, asdict
from decimal import Decimal

from . import stats

SCHEMA_VERSION = 1
EPS = sys.float_info.epsilon

PASS = "PASS"
FAIL = "FAIL"
ERROR = "ERROR"


# ----------------------------------------------------------------- model

@dataclass
class DistSummary:
    mean: float
    std: float
    n: int
    min: float
    max: float

    @classmethod
    def from_samples(cls, samples) -> "DistSummary":
        mean, std, n, lo, hi = stats.summarize(samples)
        return cls(mean=mean, std=std, n=n, min=lo, max=hi)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DistSummary":
        return cls(mean=d["mean"], std=d["std"], n=int(d["n"]),
                   min=d["min"], max=d["max"])


@dataclass
class Baseline:
    name: str
    kind: str                      # "deterministic" | "randomized"
    reference_value: str           # decimal string of the reference solution
    reference_hash: str            # sha256 of reference_value (drift guard)
    abs_error: DistSummary
    rel_error: DistSummary
    iterations: DistSummary
    elapsed_median: float
    created: str = ""
    platform: str = ""
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA_VERSION,
            "name": self.name,
            "kind": self.kind,
            "reference": {
                "value": self.reference_value,
                "sha256": self.reference_hash,
            },
            "abs_error": self.abs_error.to_dict(),
            "rel_error": self.rel_error.to_dict(),
            "iterations": self.iterations.to_dict(),
            "elapsed_median": self.elapsed_median,
            "created": self.created,
            "platform": self.platform,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Baseline":
        if d.get("schema") != SCHEMA_VERSION:
            raise ValueError(f"unsupported baseline schema: {d.get('schema')}")
        return cls(
            name=d["name"],
            kind=d["kind"],
            reference_value=d["reference"]["value"],
            reference_hash=d["reference"]["sha256"],
            abs_error=DistSummary.from_dict(d["abs_error"]),
            rel_error=DistSummary.from_dict(d["rel_error"]),
            iterations=DistSummary.from_dict(d["iterations"]),
            elapsed_median=float(d["elapsed_median"]),
            created=d.get("created", ""),
            platform=d.get("platform", ""),
            meta=d.get("meta", {}),
        )


@dataclass
class Tolerances:
    alpha: float = 0.01          # significance level (false-positive budget)
    rel_tol: float = 0.05        # allowed relative growth of mean error
    abs_tol: float = 0.0         # extra absolute slack on the error limit
    z_min: float = 4.0           # min sigma multiplier (deterministic)
    noise_ulps: float = 64.0     # platform noise floor, in eps*scale units
    delta_rel: float = 0.10      # min meaningful degradation, fraction of
                                 # baseline mean error (randomized tests)
    delta_abs: float = 0.0       # min meaningful degradation, absolute
    iter_rel_tol: float = 0.25   # allowed iteration growth over baseline
    iter_abs: int = 2            # extra absolute iteration slack
    time_rel_tol: float = 1.0    # warn when slower by more than 1+this (x2)
    min_samples: int = 5         # below this, flag low statistical confidence


@dataclass
class CheckOutcome:
    metric: str                  # "abs_error" | "iterations" | "elapsed" | ...
    passed: bool
    level: str                   # "fail" | "warn" | "info"
    observed: float
    baseline_mean: float
    limit: float
    detail: str


@dataclass
class CaseResult:
    name: str
    status: str                  # PASS | FAIL | ERROR
    kind: str
    checks: list = field(default_factory=list)     # list[CheckOutcome]
    warnings: list = field(default_factory=list)   # list[str]
    n_trials: int = 0
    elapsed_total: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "kind": self.kind,
            "n_trials": self.n_trials,
            "elapsed_total": self.elapsed_total,
            "error": self.error,
            "warnings": list(self.warnings),
            "checks": [asdict(c) for c in self.checks],
        }


# ------------------------------------------------------------- utilities

def hash_reference(reference_value: str) -> str:
    return hashlib.sha256(reference_value.encode("ascii")).hexdigest()


def error_against_reference(value: float, reference: Decimal):
    """Absolute/relative error of ``value`` vs a high-precision reference.

    ``Decimal(value)`` is exact for IEEE doubles, so errors far below
    float epsilon are still measured correctly.
    """
    diff = abs(Decimal(value) - reference)
    abs_err = float(diff)
    scale = abs(reference)
    if scale > 0:
        rel_err = float(diff / scale)
    else:
        rel_err = abs_err
    return abs_err, rel_err


def noise_floor(tol: Tolerances, reference: Decimal) -> float:
    """Absolute-error floor below which platform float noise lives."""
    scale = max(1.0, float(abs(reference)))
    return tol.noise_ulps * EPS * scale


# ------------------------------------------------------------- comparisons

def check_deterministic_error(observed: float, base: DistSummary,
                              tol: Tolerances, reference: Decimal,
                              metric: str = "abs_error") -> CheckOutcome:
    """One-sided limit test for a (nearly) deterministic error metric."""
    floor = noise_floor(tol, reference)
    sigma_eff = max(base.std, floor)
    if base.n >= 2:
        # Prediction limit for one new observation: the sqrt(1+1/n)
        # factor accounts for the baseline mean being estimated from
        # the same n samples (without it, few-sample baselines raise
        # far more false alarms than alpha).
        mult = max(tol.z_min,
                   stats.t_critical(base.n - 1, tol.alpha)
                   * math.sqrt(1.0 + 1.0 / base.n))
    else:
        mult = max(tol.z_min,
                   stats.t_critical(1, tol.alpha) * math.sqrt(2.0))
    limit = base.mean * (1.0 + tol.rel_tol) + tol.abs_tol + mult * sigma_eff
    passed = observed <= limit
    z = (observed - base.mean) / sigma_eff if sigma_eff > 0 else 0.0
    detail = (f"observed={observed:.6e} baseline={base.mean:.6e}"
              f"+-{base.std:.2e} (n={base.n}) limit={limit:.6e}"
              f" z={z:+.2f} sigma_eff={sigma_eff:.2e}")
    return CheckOutcome(metric=metric, passed=passed,
                        level="fail" if not passed else "info",
                        observed=observed, baseline_mean=base.mean,
                        limit=limit, detail=detail)


def check_randomized_error(new: DistSummary, base: DistSummary,
                           tol: Tolerances, reference: Decimal,
                           metric: str = "abs_error") -> CheckOutcome:
    """One-sided Welch test with a non-inferiority margin.

    Fails only when degradation is both statistically significant and
    practically meaningful (lower confidence bound on the mean
    difference exceeds delta).  False-positive rate <= tol.alpha.
    """
    floor = noise_floor(tol, reference)
    var0 = max(base.std, floor) ** 2
    var1 = max(new.std, floor) ** 2
    delta = max(tol.delta_abs, tol.delta_rel * abs(base.mean))
    diff = new.mean - base.mean
    se = math.sqrt(var0 / base.n + var1 / new.n)
    df = stats.welch_df(var0, base.n, var1, new.n)
    crit = stats.t_critical(df, tol.alpha)
    lower_bound = diff - crit * se          # (1-alpha) lower conf. bound
    t_stat = (diff - delta) / se if se > 0 else 0.0
    passed = lower_bound <= delta
    detail = (f"mean={new.mean:.6e} vs baseline={base.mean:.6e}"
              f" (n={new.n}/{base.n}) diff={diff:+.3e}"
              f" delta={delta:.3e} t={t_stat:+.2f} crit={crit:.2f}"
              f" df={df:.1f} lower_bound={lower_bound:+.3e}")
    return CheckOutcome(metric=metric, passed=passed,
                        level="fail" if not passed else "info",
                        observed=new.mean, baseline_mean=base.mean,
                        limit=base.mean + delta + crit * se,
                        detail=detail)


def check_iterations(new: DistSummary, base: DistSummary,
                     tol: Tolerances) -> CheckOutcome:
    """Guard against silently buying accuracy with more iterations."""
    limit = (base.mean * (1.0 + tol.iter_rel_tol)
             + tol.iter_abs + tol.z_min * base.std)
    passed = new.mean <= limit
    detail = (f"mean_iters={new.mean:.2f} baseline={base.mean:.2f}"
              f"+-{base.std:.2f} (n={base.n}) limit={limit:.2f}")
    return CheckOutcome(metric="iterations", passed=passed,
                        level="fail" if not passed else "info",
                        observed=new.mean, baseline_mean=base.mean,
                        limit=limit, detail=detail)


def check_elapsed(new_median: float, base_median: float,
                  tol: Tolerances) -> CheckOutcome:
    """Wall-clock comparison.  Timing is noisy, so this only warns."""
    limit = base_median * (1.0 + tol.time_rel_tol)
    passed = new_median <= limit
    ratio = new_median / base_median if base_median > 0 else math.inf
    detail = (f"median={new_median * 1e6:.1f}us baseline="
              f"{base_median * 1e6:.1f}us ratio={ratio:.2f}"
              f" limit={limit * 1e6:.1f}us")
    return CheckOutcome(metric="elapsed", passed=passed,
                        level="info" if passed else "warn",
                        observed=new_median, baseline_mean=base_median,
                        limit=limit, detail=detail)
