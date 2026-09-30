"""差异报告：执行计划 vs 实际执行结果。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from .executor import RunResult
from .planner import Plan


@dataclass(frozen=True)
class ReportRow:
    name: str
    planned_start: float
    planned_end: float
    actual_start: float
    actual_end: float
    attempts: int
    status: str  # completed / rolled_back / failed / skipped
    slow: bool


@dataclass(frozen=True)
class DiffReport:
    rows: List[ReportRow]
    slow_steps: List[ReportRow]
    skipped_steps: List[str]
    rolled_back_steps: List[str]
    failed_step: str | None


def build_report(
    plan: Plan, result: RunResult, slow_factor: float = 1.5
) -> DiffReport:
    planned: Dict[str, Tuple[float, float]] = {}
    for wave in plan.waves:
        for name in wave.steps:
            planned[name] = (wave.start, wave.end)

    rows: List[ReportRow] = []
    failed_step = None
    for name in plan.order:
        planned_start, planned_end = planned[name]
        record = result.records.get(name)
        if record is None or name in result.skipped:
            rows.append(
                ReportRow(
                    name, planned_start, planned_end,
                    float("nan"), float("nan"), 0, "skipped", False,
                )
            )
            continue
        planned_dur = planned_end - planned_start
        actual_dur = record.end - record.start
        slow = record.status != "failed" and actual_dur > planned_dur * slow_factor
        if record.status == "failed":
            failed_step = name
        rows.append(
            ReportRow(
                name, planned_start, planned_end,
                record.start, record.end, record.attempts,
                record.status, slow,
            )
        )

    return DiffReport(
        rows=rows,
        slow_steps=[r for r in rows if r.slow],
        skipped_steps=list(result.skipped),
        rolled_back_steps=list(result.rolled_back),
        failed_step=failed_step,
    )


def render_report(report: DiffReport) -> str:
    header = (
        f"{'步骤':<14}{'计划区间':>14}{'实际区间':>14}"
        f"{'尝试':>5}  {'状态':<12}备注"
    )
    lines = [header, "-" * len(header)]

    def span(s: float, e: float) -> str:
        if s != s:  # NaN
            return "-"
        return f"{s:g}~{e:g}"

    for row in report.rows:
        note = ""
        if row.slow:
            note = "耗时异常"
        elif row.status == "skipped":
            note = "被跳过（未执行）"
        elif row.status == "rolled_back":
            note = "已回滚"
        elif row.status == "failed":
            note = "失败（重试耗尽）"
        lines.append(
            f"{row.name:<14}{span(row.planned_start, row.planned_end):>14}"
            f"{span(row.actual_start, row.actual_end):>14}"
            f"{row.attempts:>5}  {row.status:<12}{note}"
        )

    lines.append("")
    lines.append(f"耗时异常步骤: {[r.name for r in report.slow_steps] or '无'}")
    lines.append(f"被跳过步骤  : {report.skipped_steps or '无'}")
    lines.append(f"回滚覆盖步骤: {report.rolled_back_steps or '无'}")
    if report.failed_step:
        lines.append(f"失败步骤    : {report.failed_step}")
    return "\n".join(lines)
