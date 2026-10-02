"""Orchestrates all static checks and produces a structured report."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

from .analysis import ALWAYS, NEVER, OK, UNKNOWN, analyze_rule
from .duplicates import find_duplicates
from .model import Rule

NEVER_FIRES = "NEVER_FIRES"
ALWAYS_FIRES = "ALWAYS_FIRES"
MISSING_METRIC = "MISSING_METRIC"
INVALID_RULE = "INVALID_RULE"
EMPTY_RULE = "EMPTY_RULE"
DUP_EQUIVALENT = "DUPLICATE_EQUIVALENT"
DUP_OVERLAP = "DUPLICATE_OVERLAP"
DUP_CONTAINED = "DUPLICATE_CONTAINED"

SEVERITY_ORDER = {"info": 0, "warning": 1, "error": 2}


@dataclass
class Issue:
    type: str
    severity: str
    rule_ids: list
    message: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Report:
    issues: list
    verdicts: dict
    stats: dict

    def to_dict(self) -> dict:
        return {
            "stats": self.stats,
            "verdicts": {
                rid: {"status": v.status, "reasons": v.reasons}
                for rid, v in self.verdicts.items()
            },
            "issues": [i.to_dict() for i in self.issues],
        }


class Checker:
    def __init__(self, metrics, similarity_threshold: float = 0.8,
                 contained_threshold: float = 0.5):
        self.registry = {m.name: m for m in metrics}
        self.similarity_threshold = similarity_threshold
        self.contained_threshold = contained_threshold

    def check(self, rules) -> Report:
        issues: list = []
        verdicts: dict = {}
        seen_ids = set()

        for rule in rules:
            if rule.id in seen_ids:
                issues.append(Issue(
                    INVALID_RULE, "error", [rule.id],
                    f"duplicate rule id {rule.id!r}", {},
                ))
            seen_ids.add(rule.id)

            if not rule.conditions:
                issues.append(Issue(
                    EMPTY_RULE, "error", [rule.id],
                    "rule has no conditions", {},
                ))
                continue

            analysis = analyze_rule(rule, self.registry)
            verdicts[rule.id] = analysis

            if analysis.status == NEVER:
                issues.append(Issue(
                    NEVER_FIRES, "error", [rule.id],
                    "rule can never fire",
                    {"reasons": analysis.reasons,
                     "conditions": [c.render() for c in rule.conditions]},
                ))
            elif analysis.status == ALWAYS:
                issues.append(Issue(
                    ALWAYS_FIRES, "error", [rule.id],
                    "rule always fires",
                    {"reasons": analysis.reasons,
                     "conditions": [c.render() for c in rule.conditions]},
                ))

            missing = sorted({
                cv.condition.metric
                for cv in analysis.condition_verdicts
                if cv.status == UNKNOWN
            })
            for name in missing:
                issues.append(Issue(
                    MISSING_METRIC, "warning", [rule.id],
                    f"condition references unknown metric {name!r}",
                    {"metric": name,
                     "known_metrics": sorted(self.registry)},
                ))

        for r1, r2, sim in find_duplicates(
            rules, self.registry,
            self.similarity_threshold, self.contained_threshold,
        ):
            if sim.kind == "equivalent":
                itype, severity = DUP_EQUIVALENT, "warning"
                message = f"{r1.id} and {r2.id} are equivalent rules"
            elif sim.kind == "contained":
                itype, severity = DUP_CONTAINED, "info"
                message = f"{r1.id} / {r2.id}: one rule always fires with the other"
            else:
                itype, severity = DUP_OVERLAP, "warning"
                message = f"{r1.id} and {r2.id} have highly overlapping trigger regions"
            issues.append(Issue(
                itype, severity, [r1.id, r2.id], message,
                {"similarity": round(sim.score, 4),
                 "containment": round(sim.containment, 4),
                 "kind": sim.kind,
                 "basis": sim.detail},
            ))

        counts: dict = {}
        for issue in issues:
            counts[issue.type] = counts.get(issue.type, 0) + 1
        stats = {
            "rules_checked": len(rules),
            "issues_total": len(issues),
            "by_type": counts,
            "never_fires": counts.get(NEVER_FIRES, 0),
            "always_fires": counts.get(ALWAYS_FIRES, 0),
            "duplicates": (
                counts.get(DUP_EQUIVALENT, 0)
                + counts.get(DUP_OVERLAP, 0)
                + counts.get(DUP_CONTAINED, 0)
            ),
        }
        return Report(issues, verdicts, stats)
