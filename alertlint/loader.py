"""JSON loaders with validation.  Bad entries become structured errors
instead of crashing the whole check."""

from __future__ import annotations

import json

from .model import ALL_, Condition, MetricMeta, Rule


def load_metrics(path: str):
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    metrics = []
    for item in data.get("metrics", []):
        metrics.append(MetricMeta(
            name=item["name"],
            min=float(item["min"]),
            max=float(item["max"]),
            unit=item.get("unit", ""),
            sample_interval_seconds=(
                float(item["sample_interval_seconds"])
                if item.get("sample_interval_seconds") is not None
                else None
            ),
        ))
    return metrics


def load_rules(path: str):
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    rules, errors = [], []
    for idx, item in enumerate(data.get("rules", [])):
        rid = item.get("id", f"<rule#{idx}>")
        try:
            conditions = [
                Condition(
                    metric=c["metric"],
                    op=c["op"],
                    threshold=float(c["threshold"]),
                    agg=c.get("agg", "avg"),
                    window_seconds=float(c.get("window_seconds", 60)),
                )
                for c in item.get("conditions", [])
            ]
            rules.append(Rule(
                id=rid,
                name=item.get("name", rid),
                conditions=conditions,
                combinator=item.get("combinator", ALL_),
                severity=item.get("severity", "warning"),
                enabled=bool(item.get("enabled", True)),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            errors.append({"rule_id": rid, "error": str(exc)})
    return rules, errors
