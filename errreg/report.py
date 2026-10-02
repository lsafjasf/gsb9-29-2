"""Regression report rendering: Markdown (human) + JSON (machine)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .core import PASS, FAIL, ERROR


def write_reports(results, out_dir, label: str = "error-regression") -> dict:
    paths = {}
    data = {
        "label": label,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": _summary(results),
        "cases": [r.to_dict() for r in results],
    }
    json_path = out_dir / f"{label}.json"
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    paths["json"] = json_path
    md_path = out_dir / f"{label}.md"
    md_path.write_text(_render_markdown(data), encoding="utf-8")
    paths["markdown"] = md_path
    return paths


def _summary(results) -> dict:
    counts = {PASS: 0, FAIL: 0, ERROR: 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    overall = PASS
    if counts.get(ERROR):
        overall = ERROR
    if counts.get(FAIL):
        overall = FAIL
    return {"overall": overall, "passed": counts[PASS],
            "failed": counts[FAIL], "errors": counts[ERROR],
            "total": len(results)}


def _render_markdown(data: dict) -> str:
    s = data["summary"]
    lines = [
        f"# Error Regression Report: {data['label']}",
        "",
        f"Generated: {data['generated']}",
        "",
        f"**Overall: {s['overall']}** "
        f"({s['passed']} passed, {s['failed']} failed, "
        f"{s['errors']} errors, {s['total']} total)",
        "",
        "| Status | Case | Kind | Trials | Metric detail |",
        "| --- | --- | --- | --- | --- |",
    ]
    for case in data["cases"]:
        detail_parts = []
        for check in case["checks"]:
            mark = "FAIL " if (not check["passed"] and check["level"] == "fail") \
                else ("warn " if not check["passed"] else "")
            detail_parts.append(f"`{mark}{check['metric']}: {check['detail']}`")
        if case["error"]:
            detail_parts.append(f"**error:** {case['error']}")
        detail = "<br>".join(detail_parts) if detail_parts else ""
        lines.append(
            f"| {case['status']} | `{case['name']}` | {case['kind']} "
            f"| {case['n_trials']} | {detail} |")
        if case["warnings"]:
            for w in case["warnings"]:
                lines.append(
                    f"|  |  |  |  | ⚠ {w} |")
    lines.append("")
    lines.extend([
        "## Legend",
        "",
        "- `abs_error[max]`: worst absolute error over fresh trials vs "
        "the statistical limit from the baseline (deterministic cases).",
        "- `abs_error[mean]`: Welch one-sided test of the mean error "
        "against the non-inferiority margin `delta` (randomized cases); "
        "fails only when the lower (1-alpha) confidence bound on "
        "degradation exceeds `delta`.",
        "- `iterations`: failure stops accuracy being bought silently "
        "with more iterations.",
        "- `elapsed`: warning only; wall-clock noise is not treated "
        "as a correctness regression.",
        "",
    ])
    return "\n".join(lines)


def print_console(results) -> int:
    for r in results:
        print(f"[{r.status}] {r.name} ({r.kind}, {r.n_trials} trials)")
        for check in r.checks:
            tag = "" if check.passed else f" <{check.level.upper()}>"
            print(f"    {check.metric}: {check.detail}{tag}")
        for w in r.warnings:
            print(f"    WARNING: {w}")
        if r.error:
            print(f"    ERROR: {r.error}")
    s = _summary(results)
    print(f"\nOverall: {s['overall']} "
          f"({s['passed']} passed / {s['failed']} failed / "
          f"{s['errors']} errors, {s['total']} total)")
    return 0 if s["overall"] == PASS else 1
