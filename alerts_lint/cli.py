"""命令行入口。

用法:
    python3 -m alerts_lint.cli --rules rules.json --metrics metrics.json
    python3 -m alerts_lint.cli --rules rules.json --metrics metrics.json \\
        --json --out reports/report.json
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List

from .lint import lint_rules
from .model import load_metrics, load_rules

SEVERITY_MARK = {"error": "✗", "warning": "⚠", "info": "ℹ"}


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def render_text(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    summary = report["summary"]
    lines.append("告警规则静态检查报告")
    lines.append("=" * 48)
    lines.append(
        f"规则总数 {summary['total_rules']}｜恒不触发 {summary['never_triggers']}｜"
        f"恒触发 {summary['always_triggers']}｜无法判定 {summary['inconclusive']}｜"
        f"正常 {summary['normal']}"
    )
    lines.append(
        f"重复组 {summary['duplicate_groups']}｜错误 {summary['errors']}｜"
        f"警告 {summary['warnings']}"
    )
    lines.append("")
    for finding in report["findings"]:
        mark = SEVERITY_MARK.get(finding["severity"], "·")
        target = finding["rule_id"] or ", ".join(finding["rule_ids"])
        lines.append(f"{mark} [{finding['severity'].upper()}] {finding['category']}：{target}")
        basis = finding["basis"]
        if basis and isinstance(basis[0], dict):
            for item in basis:
                lines.append(
                    f"    - {' ↔ '.join(item['pair'])}（{item['kind']}，"
                    f"相似度 {item['similarity']}）"
                )
                for detail in item["detail"]:
                    lines.append(f"        {detail}")
        else:
            for item in basis:
                lines.append(f"    依据: {item}")
        lines.append("")
    return "\n".join(lines)


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(description="告警规则静态检查器")
    parser.add_argument("--rules", required=True, help="告警规则 JSON 文件")
    parser.add_argument("--metrics", required=True, help="指标元数据 JSON 文件")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--out", help="结果写入文件（默认 stdout）")
    args = parser.parse_args(argv)

    try:
        rules_raw = _load_json(args.rules)
        metrics_raw = _load_json(args.metrics)
        if isinstance(rules_raw, dict):
            rules_raw = rules_raw.get("rules", [])
        if isinstance(metrics_raw, dict):
            metrics_raw = metrics_raw.get("metrics", [])
        report = lint_rules(load_rules(rules_raw), load_metrics(metrics_raw))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"输入错误: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_text(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + ("\n" if not args.json else ""))
    else:
        print(text)
    return 1 if report["summary"]["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
