"""差异报告：计划 vs 实际、耗时异常、跳过、回滚覆盖。"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from .model import FAILED, SKIPPED, RunResult, Step, StepReport
from .planner import Plan


def slow_steps(steps: Dict[str, StepReport],
               step_defs: Dict[str, Step],
               factor: float,
               min_delta: float) -> List[Dict[str, Any]]:
    """实际耗时超过预估 factor 倍且超出 min_delta 秒的步骤。"""
    slow = []
    for sid, report in steps.items():
        if report.status not in ("SUCCESS", FAILED):
            continue
        estimate = step_defs[sid].duration_estimate
        threshold = estimate * factor
        if report.duration > threshold and report.duration - estimate > min_delta:
            slow.append({
                "id": sid,
                "estimated": round(estimate, 6),
                "actual": round(report.duration, 6),
                "ratio": round(report.duration / estimate, 2)
                if estimate > 0 else float("inf"),
            })
    slow.sort(key=lambda item: -item["actual"])
    return slow


def build_report(result: RunResult, plan: Plan) -> Dict[str, Any]:
    """汇总一次运行的差异报告（可 JSON 序列化）。"""
    executed_set = set(result.execution_order)
    plan_prefix = [sid for sid in result.plan_order if sid in executed_set]
    rollback_steps = [
        {"id": rec.id, "status": rec.status,
         "duration": round(rec.duration, 6)}
        for rec in result.rollback
    ]
    report = {
        "run_status": result.status,
        "order": {
            "planned": result.plan_order,
            "actual": result.execution_order,
            "matches_plan": plan_prefix == result.execution_order,
        },
        "steps": [
            {
                "id": rep.id,
                "status": rep.status,
                "attempts": rep.attempts,
                "duration": round(rep.duration, 6),
                "planned_wave": rep.planned_wave,
                "planned_start": rep.planned_start,
                "skipped_reason": rep.skipped_reason,
                "error": rep.error,
                "rollback_status": rep.rollback_status,
            }
            for rep in (result.steps[sid] for sid in result.plan_order)
        ],
        "slow_steps": result.slow_steps,
        "skipped": [
            {"id": sid, "reason": result.steps[sid].skipped_reason}
            for sid in result.skipped
        ],
        "rollback": {
            "triggered": bool(result.rollback),
            "covered_steps": rollback_steps,
        },
        "effects": result.effect_ledger,
        "estimated_makespan": plan.estimated_makespan,
    }
    return report


def render_text(report: Dict[str, Any]) -> str:
    lines = []
    lines.append("== 运行状态: %s ==" % report["run_status"])
    lines.append("计划顺序: %s" % " -> ".join(report["order"]["planned"]))
    actual = report["order"]["actual"] or ["(无)"]
    lines.append("实际顺序: %s" % " -> ".join(actual))
    lines.append("顺序与计划一致: %s" % report["order"]["matches_plan"])
    lines.append("-- 步骤明细 --")
    for step in report["steps"]:
        line = "  %-16s %-14s attempts=%d duration=%.3fs" % (
            step["id"], step["status"], step["attempts"], step["duration"])
        if step["error"]:
            line += " error=%s" % step["error"]
        if step["skipped_reason"]:
            line += " (%s)" % step["skipped_reason"]
        lines.append(line)
    lines.append("-- 耗时异常 --")
    if report["slow_steps"]:
        for item in report["slow_steps"]:
            lines.append("  %s: 预估 %.3fs 实际 %.3fs (%.1fx)" % (
                item["id"], item["estimated"], item["actual"], item["ratio"]))
    else:
        lines.append("  (无)")
    lines.append("-- 被跳过 --")
    if report["skipped"]:
        for item in report["skipped"]:
            lines.append("  %s (%s)" % (item["id"], item["reason"]))
    else:
        lines.append("  (无)")
    lines.append("-- 回滚覆盖 --")
    if report["rollback"]["triggered"]:
        for item in report["rollback"]["covered_steps"]:
            lines.append("  %s -> %s" % (item["id"], item["status"]))
    else:
        lines.append("  (未触发)")
    return "\n".join(lines)


def save_report(report: Dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
