"""Compare fresh measurements against stored baselines."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .baseline import CaseBaseline
from .stats import degradation_limit, summarize


@dataclass
class Tolerances:
    """Knobs controlling how much wiggle a case is allowed.

    rel_tol:     deterministic cases -- allowed relative growth of the error
                 (absorbs last-ulp / libm differences across platforms).
    abs_floor:   deterministic cases -- absolute slack added to the limit
                 (needed when the baseline error is exactly 0).
    iter_slack:  allowed relative growth of the iteration count.
    time_factor: warn when mean runtime grows beyond this factor
                 (and beyond mean + 3 sigma).
    """

    rel_tol: float = 0.05
    abs_floor: float = 0.0
    iter_slack: float = 0.10
    time_factor: float = 2.0

    def merged(self, overrides: Optional[dict]) -> "Tolerances":
        values = dict(self.__dict__)
        for key, value in (overrides or {}).items():
            if key not in values:
                raise KeyError(f"unknown tolerance key: {key!r}")
            values[key] = value
        return Tolerances(**values)


@dataclass
class SubCheck:
    name: str
    ok: bool
    severity: str  # "fail" | "warn"
    summary: str
    data: dict = field(default_factory=dict)


@dataclass
class CaseResult:
    name: str
    kind: str
    status: str  # "PASS" | "WARN" | "FAIL"
    checks: List[SubCheck] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "checks": [
                {
                    "name": c.name,
                    "ok": c.ok,
                    "severity": c.severity,
                    "summary": c.summary,
                    "data": c.data,
                }
                for c in self.checks
            ],
        }


def check_case(
    base: CaseBaseline,
    errors: Sequence[float],
    iterations: Sequence[float],
    times_ms: Sequence[float],
    defaults: Optional[Tolerances] = None,
) -> CaseResult:
    tols = (defaults or Tolerances()).merged(base.tolerances)
    error_check = _check_error(base, errors, tols)
    iter_check = _check_iterations(base, iterations, tols)
    time_check = _check_timing(base, times_ms, tols)

    # Flag "bought accuracy with more iterations": error improved while the
    # iteration budget was exceeded.
    if (not iter_check.ok
            and error_check.data.get("observed", 1.0) < error_check.data["baseline"]["mean"]):
        iter_check.summary += "  [error improved, but only by spending more iterations]"
        iter_check.data["accuracy_bought_with_iterations"] = True

    checks = [error_check, iter_check, time_check]
    status = "PASS"
    for c in checks:
        if not c.ok and c.severity == "fail":
            status = "FAIL"
        elif not c.ok and status == "PASS":
            status = "WARN"
    return CaseResult(name=base.name, kind=base.kind, status=status, checks=checks)


def _check_error(base: CaseBaseline, errors: Sequence[float],
                 tols: Tolerances) -> SubCheck:
    b = summarize(base.errors)
    c = summarize(errors)
    deterministic = base.kind == "deterministic" or b.n < 2 or b.stdev == 0.0

    if deterministic:
        slack = max(tols.abs_floor, tols.rel_tol * b.mean)
        limit = b.mean + slack
        observed = c.maximum
        ok = observed <= limit
        z = None
        mode = "deterministic"
        base_txt = f"base {b.mean:.3e} (n={b.n})"
        cur_txt = f"cur {observed:.3e}"
    else:
        limit = degradation_limit(b, c.n)
        if tols.abs_floor:
            limit = max(limit, b.mean + tols.abs_floor)
        observed = c.mean
        se = b.stdev * math.sqrt(1.0 / b.n + 1.0 / c.n)
        z = (c.mean - b.mean) / se
        ok = observed <= limit
        mode = "statistical"
        base_txt = f"base {b.mean:.3e} +/- {b.stdev:.3e} (n={b.n})"
        cur_txt = f"cur {observed:.3e} (n={c.n}) z={z:+.2f}"

    exceedance = observed - limit
    verdict = "ok  " if ok else "FAIL"
    summary = (
        f"{base_txt}  {cur_txt}  limit {limit:.3e}  "
        f"{'exceedance' if exceedance > 0 else 'margin'} {exceedance:+.3e}"
    )
    return SubCheck(
        name="error",
        ok=ok,
        severity="fail",
        summary=summary,
        data={
            "mode": mode,
            "baseline": b.as_dict(),
            "current": c.as_dict(),
            "observed": observed,
            "limit": limit,
            "exceedance": exceedance,
            "z_score": z,
            "verdict": verdict.strip(),
        },
    )


def _check_iterations(base: CaseBaseline, iterations: Sequence[float],
                      tols: Tolerances) -> SubCheck:
    b = summarize(base.iterations)
    c = summarize(iterations)
    limit = max(b.maximum, math.ceil(b.mean * (1.0 + tols.iter_slack)))
    observed = c.maximum
    ok = observed <= limit
    summary = (
        f"base mean {b.mean:.1f} max {b.maximum:g}  "
        f"cur {observed:g}  limit {limit:g}"
    )
    return SubCheck(
        name="iterations",
        ok=ok,
        severity="fail",
        summary=summary,
        data={
            "baseline": b.as_dict(),
            "current": c.as_dict(),
            "observed": observed,
            "limit": limit,
        },
    )


def _check_timing(base: CaseBaseline, times_ms: Sequence[float],
                  tols: Tolerances) -> SubCheck:
    b = summarize(base.times_ms)
    c = summarize(times_ms)
    limit = max(b.mean * tols.time_factor, b.mean + 3.0 * b.stdev)
    observed = c.mean
    ok = observed <= limit
    summary = (
        f"base {b.mean:.3f}ms +/- {b.stdev:.3f}  "
        f"cur {observed:.3f}ms  warn-limit {limit:.3f}ms"
    )
    return SubCheck(
        name="timing",
        ok=ok,
        severity="warn",
        summary=summary,
        data={
            "baseline": b.as_dict(),
            "current": c.as_dict(),
            "observed": observed,
            "limit": limit,
        },
    )
