"""Command line entry point: python3 -m alertlint ..."""

from __future__ import annotations

import argparse
import json
import sys

from .checker import Checker, SEVERITY_ORDER
from .loader import load_metrics, load_rules


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alertlint",
        description="Static checker for alert-rule triggerability and duplicates.",
    )
    parser.add_argument("--metrics", required=True,
                        help="JSON file with metric value ranges")
    parser.add_argument("--rules", action="append", required=True,
                        help="JSON file with alert rules (repeatable)")
    parser.add_argument("--similarity-threshold", type=float, default=0.8,
                        help="Jaccard threshold for overlap findings (default 0.8)")
    parser.add_argument("--contained-threshold", type=float, default=0.5,
                        help="condition-set Jaccard threshold for containment findings")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--fail-at", choices=("never", "info", "warning", "error"),
                        default="warning",
                        help="minimum severity that yields a non-zero exit code")
    return parser


def render_text(report, load_errors) -> str:
    lines = []
    stats = report.stats
    lines.append(
        f"checked {stats['rules_checked']} rules, "
        f"found {stats['issues_total']} issues "
        f"(never={stats['never_fires']}, always={stats['always_fires']}, "
        f"duplicates={stats['duplicates']})"
    )
    for err in load_errors:
        lines.append(f"[error] LOAD_ERROR {err['rule_id']}: {err['error']}")
    for issue in report.issues:
        ids = ", ".join(issue.rule_ids)
        lines.append(f"[{issue.severity}] {issue.type} rules={ids}")
        lines.append(f"    {issue.message}")
        evidence = issue.evidence
        if "similarity" in evidence:
            lines.append(
                f"    similarity={evidence['similarity']} "
                f"containment={evidence['containment']}"
            )
            lines.append(f"    basis: {evidence['basis']}")
        if evidence.get("reasons"):
            for reason in evidence["reasons"]:
                lines.append(f"    - {reason}")
        if evidence.get("metric"):
            lines.append(f"    - known metrics: {', '.join(evidence['known_metrics'])}")
    return "\n".join(lines)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    metrics = load_metrics(args.metrics)
    rules, load_errors = [], []
    for path in args.rules:
        path_rules, path_errors = load_rules(path)
        rules.extend(path_rules)
        load_errors.extend(path_errors)

    checker = Checker(
        metrics,
        similarity_threshold=args.similarity_threshold,
        contained_threshold=args.contained_threshold,
    )
    report = checker.check(rules)

    if args.format == "json":
        payload = report.to_dict()
        payload["load_errors"] = load_errors
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_text(report, load_errors))

    fail_level = SEVERITY_ORDER[args.fail_at]
    max_level = max(
        (SEVERITY_ORDER[i.severity] for i in report.issues),
        default=0,
    )
    if load_errors:
        max_level = max(max_level, SEVERITY_ORDER["error"])
    return 1 if max_level >= fail_level and args.fail_at != "never" else 0


if __name__ == "__main__":
    sys.exit(main())
