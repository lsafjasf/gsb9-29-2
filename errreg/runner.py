"""Runs problem cases, builds baselines, and performs regression checks.

A *case* is anything providing:

* ``name``            : unique case identifier
* ``kind``            : "deterministic" or "randomized"
* ``reference_fn()``  : returns the high-precision decimal.Decimal
                        reference solution
* ``trial_fn(rng)``   : returns ``(value, iterations)`` where value is
                        a float and iterations is the number of
                        algorithm iterations consumed; ``rng`` is a
                        seeded random.Random (used for input ulp
                        jitter in deterministic cases and for the
                        algorithm's own randomness otherwise)
* ``baseline_trials`` : samples used when building the baseline
* ``check_trials``    : samples used for each regression check
"""

from __future__ import annotations

import platform
import random
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from .core import (Baseline, CaseResult, DistSummary,
                   Tolerances, PASS, FAIL, ERROR,
                   check_deterministic_error, check_randomized_error,
                   check_iterations, check_elapsed,
                   error_against_reference, hash_reference)
from .baseline import save_baseline, load_baseline, has_baseline

SEED_STEP = 0x9E3779B1


@dataclass
class Case:
    name: str
    kind: str
    reference_fn: Callable[[], Decimal]
    trial_fn: Callable[[random.Random], tuple]
    baseline_name: str = None      # defaults to name; variants of the
                                   # same problem share one baseline
    baseline_trials: int = 30
    check_trials: int = 30
    seed_base: int = 0xC0FFEE
    meta: dict = None

    def __post_init__(self):
        if self.kind not in ("deterministic", "randomized"):
            raise ValueError(f"unknown case kind: {self.kind!r}")
        if self.baseline_name is None:
            self.baseline_name = self.name
        if self.meta is None:
            self.meta = {}


@dataclass
class _Trial:
    abs_error: float
    rel_error: float
    iterations: int
    elapsed: float


def _run_trials(case: Case, n: int, seed_offset: int = 0) -> list:
    trials = []
    for i in range(n):
        rng = random.Random(case.seed_base + seed_offset + i * SEED_STEP)
        start = time.perf_counter()
        value, iterations = case.trial_fn(rng)
        elapsed = time.perf_counter() - start
        trials.append((value, int(iterations), elapsed))
    return trials


def _to_trials(raw, reference: Decimal) -> list:
    out = []
    for value, iterations, elapsed in raw:
        abs_err, rel_err = error_against_reference(value, reference)
        out.append(_Trial(abs_err, rel_err, iterations, elapsed))
    return out


class Runner:
    def __init__(self, baseline_dir: str, tolerances: Tolerances = None):
        self.baseline_dir = baseline_dir
        self.tol = tolerances or Tolerances()

    def build_baseline(self, case: Case, save: bool = True) -> Baseline:
        reference = case.reference_fn()
        ref_str = format(reference, "f")
        raw = _run_trials(case, case.baseline_trials)
        trials = _to_trials(raw, reference)
        baseline = Baseline(
            name=case.baseline_name,
            kind=case.kind,
            reference_value=ref_str,
            reference_hash=hash_reference(ref_str),
            abs_error=DistSummary.from_samples(t.abs_error for t in trials),
            rel_error=DistSummary.from_samples(t.rel_error for t in trials),
            iterations=DistSummary.from_samples(t.iterations for t in trials),
            elapsed_median=statistics.median(t.elapsed for t in trials),
            created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            platform=platform.platform(),
            meta=dict(case.meta),
        )
        if save:
            save_baseline(self.baseline_dir, baseline)
        return baseline

    def check(self, case: Case) -> CaseResult:
        result = CaseResult(name=case.name, status=PASS, kind=case.kind)
        try:
            if not has_baseline(self.baseline_dir, case.baseline_name):
                raise FileNotFoundError(
                    f"no baseline for {case.baseline_name!r}; build one first")
            baseline = load_baseline(self.baseline_dir, case.baseline_name)

            reference = case.reference_fn()
            ref_str = format(reference, "f")
            if hash_reference(ref_str) != baseline.reference_hash:
                result.status = ERROR
                result.error = ("high-precision reference solution drifted "
                                "from the value recorded in the baseline")
                return result

            raw = _run_trials(case, case.check_trials, seed_offset=0x5A17)
            trials = _to_trials(raw, reference)
            result.n_trials = len(trials)
            result.elapsed_total = sum(t.elapsed for t in trials)

            new_abs = DistSummary.from_samples(t.abs_error for t in trials)
            new_rel = DistSummary.from_samples(t.rel_error for t in trials)
            new_iters = DistSummary.from_samples(t.iterations for t in trials)
            new_elapsed = statistics.median(t.elapsed for t in trials)

            if baseline.abs_error.n < self.tol.min_samples:
                result.warnings.append(
                    f"baseline built from only {baseline.abs_error.n} "
                    f"samples (< {self.tol.min_samples}); confidence "
                    "interval widened via Student-t, gather more data")
            if case.kind == "randomized" and new_abs.n < self.tol.min_samples:
                result.warnings.append(
                    f"only {new_abs.n} fresh trials; power is low, "
                    "use more trials for a meaningful test")

            if case.kind == "deterministic":
                # Compare the worst observed error: a platform that
                # occasionally rounds badly must be tolerated through
                # the statistical limit, not hidden by a median.
                abs_outcome = check_deterministic_error(
                    new_abs.max, baseline.abs_error, self.tol, reference,
                    "abs_error[max]")
                rel_outcome = check_deterministic_error(
                    new_rel.max, baseline.rel_error, self.tol, reference,
                    "rel_error[max]")
            else:
                abs_outcome = check_randomized_error(
                    new_abs, baseline.abs_error, self.tol, reference,
                    "abs_error[mean]")
                rel_outcome = check_randomized_error(
                    new_rel, baseline.rel_error, self.tol, reference,
                    "rel_error[mean]")

            iter_outcome = check_iterations(
                new_iters, baseline.iterations, self.tol)
            time_outcome = check_elapsed(
                new_elapsed, baseline.elapsed_median, self.tol)

            result.checks.extend(
                [abs_outcome, rel_outcome, iter_outcome, time_outcome])
            if not time_outcome.passed:
                result.warnings.append(
                    f"runtime slower than tolerance: {time_outcome.detail}")
            if any(c.level == "fail" and not c.passed
                   for c in result.checks):
                result.status = FAIL
        except Exception as exc:  # surface as ERROR, not a crash
            result.status = ERROR
            result.error = f"{type(exc).__name__}: {exc}"
        return result
