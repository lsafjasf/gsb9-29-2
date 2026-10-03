"""顶层入口：对一批规则做静态检查，产出结构化问题清单。"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List

from .duplicates import find_duplicates
from .liveness import ALWAYS, NEVER, UNKNOWN, VARIABLE, analyze_rule
from .model import Metric, Rule

STATUS_LABEL = {
    NEVER: "永远不触发",
    ALWAYS: "永远触发（恒真）",
    UNKNOWN: "信息不足，无法判定",
    VARIABLE: "正常（可能触发也可能不触发）",
}


def lint_rules(rules: List[Rule], metrics: Dict[str, Metric]) -> Dict[str, Any]:
    findings: List[Dict[str, Any]] = []
    status_by_rule: Dict[str, Dict[str, Any]] = {}

    for rule in rules:
        result = analyze_rule(rule, metrics)
        status = result["status"]
        status_by_rule[rule.id] = {
            "status": status,
            "label": STATUS_LABEL[status],
            "reasons": result["reasons"],
        }

        missing = [
            v for v in result["leaf_verdicts"] if v.metric is None
        ]
        window_bad = [
            v for v in result["leaf_verdicts"] if v.window_issue
        ]
        undecidable = [
            v for v in result["leaf_verdicts"]
            if v.metric is not None and v.trigger is None
        ]

        if window_bad and status == NEVER:
            findings.append({
                "rule_id": rule.id,
                "kind": "invalid_window",
                "severity": "error",
                "category": "永远不触发",
                "basis": [v.window_issue for v in window_bad],
            })
        elif status == NEVER:
            findings.append({
                "rule_id": rule.id,
                "kind": "never_triggers",
                "severity": "error",
                "category": "永远不触发",
                "basis": result["reasons"],
            })
        elif status == ALWAYS:
            findings.append({
                "rule_id": rule.id,
                "kind": "always_triggers",
                "severity": "error",
                "category": "永远触发",
                "basis": result["reasons"],
            })

        if missing:
            findings.append({
                "rule_id": rule.id,
                "kind": "missing_metric",
                "severity": "warning",
                "category": "缺失指标",
                "basis": [f"未注册指标: {sorted({v.leaf.metric for v in missing})}"],
            })
        elif undecidable and status == UNKNOWN:
            findings.append({
                "rule_id": rule.id,
                "kind": "inconclusive",
                "severity": "warning",
                "category": "无法判定",
                "basis": result["reasons"],
            })

        if result["window_notes"]:
            findings.append({
                "rule_id": rule.id,
                "kind": "window_warning",
                "severity": "info",
                "category": "窗口提示",
                "basis": result["window_notes"],
            })

    duplicate_groups = find_duplicates(rules, metrics)
    for group in duplicate_groups:
        if group.kind == "equivalent":
            kind, severity, category = (
                "duplicate_equivalent", "error", "重复（条件等价）"
            )
        else:
            kind, severity, category = (
                "duplicate_overlap", "warning", "重复（高度重叠）"
            )
        findings.append({
            "rule_id": None,
            "rule_ids": group.rule_ids,
            "kind": kind,
            "severity": severity,
            "category": category,
            "basis": [
                {
                    "pair": [p.rule_a, p.rule_b],
                    "kind": "等价" if p.kind == "equivalent" else "高度重叠",
                    "similarity": round(p.similarity, 3),
                    "detail": p.basis,
                }
                for p in group.pairs
            ],
        })

    counts = {
        "total_rules": len(rules),
        "never_triggers": sum(
            1 for v in status_by_rule.values() if v["status"] == NEVER
        ),
        "always_triggers": sum(
            1 for v in status_by_rule.values() if v["status"] == ALWAYS
        ),
        "inconclusive": sum(
            1 for v in status_by_rule.values() if v["status"] == UNKNOWN
        ),
        "normal": sum(
            1 for v in status_by_rule.values() if v["status"] == VARIABLE
        ),
        "duplicate_groups": len(duplicate_groups),
        "errors": sum(1 for f in findings if f["severity"] == "error"),
        "warnings": sum(1 for f in findings if f["severity"] == "warning"),
    }
    return {
        "summary": counts,
        "findings": findings,
        "status_by_rule": status_by_rule,
    }
