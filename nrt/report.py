"""Render regression reports (human-readable text + machine-readable JSON)."""

from __future__ import annotations

from typing import Optional, Sequence

from .check import CaseResult

_WIDTH = 72


def render_text(results: Sequence[CaseResult], meta: Optional[dict] = None) -> str:
    meta = meta or {}
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for r in results:
        counts[r.status] += 1

    lines = ["=" * _WIDTH, "NUMERICAL ERROR REGRESSION REPORT"]
    if meta.get("generated_utc"):
        lines.append(f"generated: {meta['generated_utc']}")
    if meta.get("baseline"):
        lines.append(f"baseline:  {meta['baseline']}")
    if meta.get("simulated_degradation"):
        lines.append("NOTE: degradation was injected on purpose (demo mode)")
    lines.append(
        f"cases: {len(results)}  PASS: {counts['PASS']}  "
        f"WARN: {counts['WARN']}  FAIL: {counts['FAIL']}"
    )
    lines.append("=" * _WIDTH)

    for r in results:
        lines.append(f"[{r.status}] {r.name} ({r.kind})")
        for c in r.checks:
            mark = "ok  " if c.ok else ("FAIL" if c.severity == "fail" else "warn")
            lines.append(f"  {c.name:<11}{mark}  {c.summary}")
        lines.append("")

    overall = "FAIL" if counts["FAIL"] else ("WARN" if counts["WARN"] else "PASS")
    lines.append("-" * _WIDTH)
    lines.append(f"overall: {overall}")
    return "\n".join(lines) + "\n"


def to_json_dict(results: Sequence[CaseResult], meta: Optional[dict] = None) -> dict:
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for r in results:
        counts[r.status] += 1
    overall = "FAIL" if counts["FAIL"] else ("WARN" if counts["WARN"] else "PASS")
    return {
        "meta": meta or {},
        "overall": overall,
        "counts": counts,
        "cases": [r.as_dict() for r in results],
    }
