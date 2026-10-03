"""判定规则是否可能触发。

对每个叶子计算：
- trigger   谓词在聚合取值域内为真的部分
- falsify   谓词为假的部分
两者是否为空决定"可触发 / 可假性"。

组合条件先转成否定范式（NNF），再递归合并：
- can_true  存在一组取值让条件成立
- can_false 存在一组取值让条件不成立
- unknown   是否因指标缺失 / 聚合不可推导而信息不足

同一指标在 AND 下的多个叶子做区间求交，可发现跨叶子矛盾
（如 cpu > 90 且 cpu < 80），单叶子分析发现不了。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .intervals import (
    Domain,
    effective_domain,
    predicate_region,
)
from .model import Cond, Leaf, Metric, Rule, format_duration

NEGATE_OP = {"<": ">=", "<=": ">", ">": "<=", ">=": "<", "==": "!=", "!=": "=="}

# 结论
NEVER = "never_triggers"        # 恒不触发
ALWAYS = "always_triggers"      # 恒触发（取值域内必然成立）
UNKNOWN = "inconclusive"        # 信息不足，无法给出强结论
VARIABLE = "may_trigger"        # 可触发也可假：正常规则


@dataclass
class LeafVerdict:
    leaf: Leaf
    metric: Optional[Metric]
    trigger: Optional[Domain]
    falsify: Optional[Domain]
    domain: Optional[Domain] = None
    notes: List[str] = field(default_factory=list)
    window_issue: Optional[str] = None  # 窗口非法/超保留/短于采样
    window_note: Optional[str] = None


@dataclass
class NodeResult:
    can_true: bool
    can_false: bool
    unknown: bool
    leaf_verdicts: List[LeafVerdict] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    # 已知量的当前可行域约束（AND 路径上逐叶子收紧）。
    # 键为 (metric, agg, window_seconds)：只有同一指标、同一聚合、
    # 同一窗口才是同一个量，才能互相约束。
    constraints: Dict[tuple, Domain] = field(default_factory=dict)


def negate(cond: Cond) -> Cond:
    if cond.kind == "not":
        return cond.children[0]
    if cond.kind == "all":
        return Cond(kind="any", children=tuple(negate(c) for c in cond.children))
    if cond.kind == "any":
        return Cond(kind="all", children=tuple(negate(c) for c in cond.children))
    leaf = cond.leaf
    flipped = Leaf(
        metric=leaf.metric,
        op=NEGATE_OP[leaf.op],
        value=leaf.value,
        agg=leaf.agg,
        window_seconds=leaf.window_seconds,
        window_raw=leaf.window_raw,
        for_seconds=leaf.for_seconds,
    )
    return Cond.leaf_cond(flipped)


def to_nnf(cond: Cond) -> Cond:
    """消除 NOT：把否定下推到叶子并翻转比较符。"""
    if cond.kind == "leaf":
        return cond
    if cond.kind == "not":
        return negate(to_nnf(cond.children[0]))
    return Cond(
        kind=cond.kind,
        children=tuple(to_nnf(c) for c in cond.children),
    )


def window_issues(metric: Optional[Metric], leaf: Leaf) -> Tuple[Optional[str], Optional[str]]:
    """返回 (致命窗口问题, 提示性说明)。"""
    if leaf.window_seconds is None:
        return f"窗口无法解析: {leaf.window_raw!r}", None
    if leaf.window_seconds <= 0:
        return f"窗口必须为正数: {leaf.window_raw!r}", None
    if metric is not None:
        if metric.retention_seconds is not None and leaf.window_seconds > metric.retention_seconds:
            return (
                f"窗口 {leaf.window_raw} 超过指标 {metric.name} 的保留期"
                f"（retention={format_duration(metric.retention_seconds)}），历史数据不足以填满窗口",
                None,
            )
        if metric.every_seconds and leaf.window_seconds < metric.every_seconds:
            return None, (
                f"窗口 {leaf.window_raw} 短于采样间隔 {format_duration(metric.every_seconds)}，"
                f"窗口内可能没有样本"
            )
    return None, None


def analyze_leaf(leaf: Leaf, metrics: Dict[str, Metric]) -> LeafVerdict:
    metric = metrics.get(leaf.metric)
    notes: List[str] = []
    issue, note = window_issues(metric, leaf)
    if metric is None:
        return LeafVerdict(
            leaf=leaf, metric=None, trigger=None, falsify=None,
            notes=[f"指标 {leaf.metric!r} 未在指标注册表中声明，取值范围未知"],
            window_issue=issue, window_note=note,
        )
    domain, dnotes = effective_domain(metric, leaf)
    notes.extend(dnotes)
    if domain is None:
        return LeafVerdict(
            leaf=leaf, metric=metric, trigger=None, falsify=None,
            notes=notes, window_issue=issue, window_note=note,
        )
    trigger = predicate_region(leaf.op, leaf.value, domain)
    falsify = trigger.complement_within(domain)
    return LeafVerdict(
        leaf=leaf, metric=metric, trigger=trigger, falsify=falsify,
        domain=domain, notes=notes, window_issue=issue, window_note=note,
    )


def _leaf_reason(verdict: LeafVerdict, status: str) -> str:
    leaf = verdict.leaf
    head = f"{leaf.metric} {leaf.agg}({leaf.window_raw}) {leaf.op} {leaf.value:g}"
    if status == "missing_metric":
        return f"{head}：指标未注册，取值范围未知，无法判定"
    if status == "undecidable":
        return f"{head}：{'; '.join(verdict.notes) or '聚合取值域不可推导'}，无法判定"
    if verdict.metric is None:
        return head
    if verdict.window_issue:
        return f"{head}：{verdict.window_issue}"
    rng_desc = f"聚合后取值域 {_domain_desc(verdict.domain, verdict.metric)}"
    if status == NEVER:
        return f"{head}：{rng_desc}内没有任何取值满足条件（满足域为空）"
    if status == ALWAYS:
        return f"{head}：{rng_desc}内所有取值都满足条件（不满足域为空）"
    return f"{head}：{rng_desc}，部分取值触发、部分不触发"


def _fmt(x: Optional[float], upper: bool = False) -> str:
    if x is None:
        return "+∞" if upper else "-∞"
    return f"{x:g}"


def _domain_desc(domain: Optional[Domain], metric: Optional[Metric]) -> str:
    if domain is None:
        return "未知"
    if domain.is_empty():
        return "∅"
    parts = []
    for lo, lo_c, hi, hi_c in domain.intervals:
        parts.append(f"[{_fmt(lo)}, {_fmt(hi, upper=True)}]")
    return " ∪ ".join(parts)


def _eval_nnf(
    cond: Cond,
    metrics: Dict[str, Metric],
    verdicts: List[LeafVerdict],
    constraints: Dict[str, Domain],
) -> NodeResult:
    if cond.kind == "leaf":
        leaf = cond.leaf
        verdict = analyze_leaf(leaf, metrics)
        verdicts.append(verdict)
        result = NodeResult(can_true=False, can_false=False, unknown=False)

        if verdict.window_issue:
            result.can_false = True
            result.unknown = True
            result.reasons.append(_leaf_reason(verdict, NEVER))
            result.constraints = dict(constraints)
            return result

        if verdict.trigger is None:
            status = "missing_metric" if verdict.metric is None else "undecidable"
            result.can_true = True
            result.can_false = True
            result.unknown = True
            result.reasons.append(_leaf_reason(verdict, status))
            result.constraints = dict(constraints)
            return result

        if verdict.trigger.is_empty():
            result.can_true = False
            result.can_false = True
            result.reasons.append(_leaf_reason(verdict, NEVER))
        elif verdict.falsify.is_empty():
            result.can_true = True
            result.can_false = False
            result.reasons.append(_leaf_reason(verdict, ALWAYS))
        else:
            result.can_true = True
            result.can_false = True
            result.reasons.append(_leaf_reason(verdict, VARIABLE))

        new_constraints = dict(constraints)
        if verdict.trigger is not None and not verdict.trigger.is_empty():
            key = (leaf.metric, leaf.agg, leaf.window_seconds)
            prev = new_constraints.get(key)
            narrowed = verdict.trigger if prev is None else prev.intersect(verdict.trigger)
            if narrowed.is_empty():
                result.can_true = False
                result.reasons.append(
                    f"与同一量 {leaf.metric} {leaf.agg}({leaf.window_raw}) 的"
                    f"其他条件矛盾：可行取值域求交为空"
                )
            new_constraints[key] = narrowed
        result.constraints = new_constraints
        return result

    if cond.kind == "all":
        return _eval_all(cond, metrics, verdicts, constraints)
    if cond.kind == "any":
        return _eval_any(cond, metrics, verdicts, constraints)
    raise AssertionError("NNF 中不应出现 not")


def _eval_all(cond, metrics, verdicts, constraints) -> NodeResult:
    # AND：每个子节点独立评估；任一不可满足则整体不可满足；
    # 全部恒真（且无未知）才恒真；同指标约束求交找跨叶子矛盾。
    any_never = False
    all_always = True
    unknown = False
    merged = dict(constraints)
    reasons: List[str] = []
    for child in cond.children:
        sub = _eval_nnf(child, metrics, verdicts, merged)
        reasons.extend(sub.reasons)
        if not sub.can_true:
            any_never = True
        if sub.can_false or sub.unknown:
            all_always = False
        unknown = unknown or sub.unknown
        merged = sub.constraints
    return NodeResult(
        can_true=not any_never,
        can_false=True if all_always is False else False,
        unknown=unknown,
        leaf_verdicts=[],
        reasons=reasons,
        constraints=merged,
    )


def _eval_any(cond, metrics, verdicts, constraints) -> NodeResult:
    # OR：任一可满足即可满足；全部恒假才不可满足。
    # 可假性：所有分支同时为假才为假。对同一量（metric+agg+window）的
    # 叶子分支，把各自的 falsify 域求交；交集为空说明这些分支覆盖了
    # 整个取值域（如 x>90 或 x<=90），条件恒真。
    any_true = False
    unknown = False
    reasons: List[str] = []
    leaf_verdicts: List[LeafVerdict] = []
    nonleaf_falsifiable = True
    for child in cond.children:
        before = len(verdicts)
        sub = _eval_nnf(child, metrics, verdicts, constraints)
        reasons.extend(sub.reasons)
        if sub.can_true:
            any_true = True
        if child.kind == "leaf":
            leaf_verdicts.extend(verdicts[before:])
        elif not (sub.can_false or sub.unknown):
            nonleaf_falsifiable = False
        unknown = unknown or sub.unknown

    groups: Dict[tuple, List[LeafVerdict]] = {}
    leaves_unknown = False
    for verdict in leaf_verdicts:
        if verdict.falsify is None:
            leaves_unknown = True
            continue
        leaf = verdict.leaf
        key = (leaf.metric, leaf.agg, leaf.window_seconds)
        groups.setdefault(key, []).append(verdict)
    leaves_falsifiable = True
    for key, group in groups.items():
        joint: Optional[Domain] = None
        for verdict in group:
            joint = verdict.falsify if joint is None else joint.intersect(verdict.falsify)
        if joint is not None and joint.is_empty():
            leaves_falsifiable = False
            reasons.append(
                f"OR 分支覆盖 {key[0]} {key[1]} 的整个取值域："
                f"所有分支不可能同时为假，条件恒真"
            )
    can_false = nonleaf_falsifiable and (
        leaves_falsifiable or leaves_unknown or unknown
    )
    return NodeResult(
        can_true=any_true or unknown,
        can_false=can_false,
        unknown=unknown,
        reasons=reasons,
        constraints=dict(constraints),
    )


def classify(result: NodeResult) -> str:
    """从三值评估结果映射最终结论。"""
    if not result.can_true:
        return NEVER
    if result.can_true and not result.can_false and not result.unknown:
        return ALWAYS
    if result.unknown:
        return UNKNOWN
    return VARIABLE


def analyze_rule(rule: Rule, metrics: Dict[str, Metric]) -> dict:
    nnf = to_nnf(rule.cond)
    verdicts: List[LeafVerdict] = []
    result = _eval_nnf(nnf, metrics, verdicts, {})
    status = classify(result)
    return {
        "rule_id": rule.id,
        "status": status,
        "can_true": result.can_true,
        "can_false": result.can_false,
        "unknown": result.unknown,
        "reasons": result.reasons,
        "leaf_verdicts": verdicts,
        "window_notes": [
            v.window_note for v in verdicts if v.window_note
        ],
    }
