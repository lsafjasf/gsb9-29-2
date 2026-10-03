"""重复/高度重叠规则检测。

两层判定：
1. 等价：规范化后的条件树签名完全一致（NNF + 扁平化 + 子节点排序 +
   整数域比较符规范化，如整数域 >89 等价于 >=90）。
2. 高度重叠：触发取值域的 Jaccard 相似度（连续域长度 / 整数域个数），
   辅以窗口接近度、聚合方式/比较符/持续时长，加权后聚类。

无界域无法直接度量重合，退化为"方向一致 + 阈值相对接近度"打分，
并对方向相反的条件（如 >80 与 <80）严格压分，避免误并。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .liveness import ALWAYS, NEVER, LeafVerdict, analyze_leaf, analyze_rule, to_nnf
from .model import Cond, Leaf, Metric, Rule

EQUIVALENT = "equivalent"
OVERLAPPING = "high_overlap"
SIM_THRESHOLD = 0.92

_GT = {">", ">="}
_LT = {"<", "<="}


def _num(x: Optional[float]) -> str:
    if x is None:
        return "*"
    return f"{x + 0.0:.6g}"


def _canon_op_value(leaf: Leaf, metric: Optional[Metric]) -> Tuple[str, float]:
    """整数域规范化：>89 → >=90，<91 → <=90。"""
    op, value = leaf.op, leaf.value
    if metric is not None and metric.integer and float(value).is_integer():
        v = int(value)
        if op == ">":
            return ">=", float(v + 1)
        if op == "<":
            return "<=", float(v - 1)
    return op, value


def leaf_key(leaf: Leaf, metric: Optional[Metric]) -> str:
    op, value = _canon_op_value(leaf, metric)
    return "|".join(
        (
            leaf.metric,
            leaf.agg,
            f"w{_num(leaf.window_seconds)}",
            f"f{_num(leaf.for_seconds)}",
            op,
            _num(value),
        )
    )


def _canonical_tree(cond: Cond, metrics: Dict[str, Metric]) -> str:
    nnf = to_nnf(cond)
    return _serialize(nnf, metrics)


def _serialize(cond: Cond, metrics: Dict[str, Metric]) -> str:
    if cond.kind == "leaf":
        leaf = cond.leaf
        return leaf_key(leaf, metrics.get(leaf.metric))
    if cond.kind in ("all", "any"):
        flat: List[Cond] = []

        def walk(node: Cond) -> None:
            if node.kind == cond.kind:
                for ch in node.children:
                    walk(ch)
            else:
                flat.append(node)

        walk(cond)
        if len(flat) == 1:
            return _serialize(flat[0], metrics)
        parts = sorted(_serialize(ch, metrics) for ch in flat)
        joiner = "&" if cond.kind == "all" else "|"
        return "(" + joiner.join(parts) + ")"
    raise AssertionError("规范化后的树不应含 not")


@dataclass
class PairDetail:
    rule_a: str
    rule_b: str
    kind: str          # equivalent / high_overlap
    similarity: float
    basis: List[str] = field(default_factory=list)


@dataclass
class DuplicateGroup:
    rule_ids: List[str]
    kind: str
    max_similarity: float
    pairs: List[PairDetail] = field(default_factory=list)


# ---------------- 叶子相似度 ----------------

def _threshold_proximity(v1: float, v2: float) -> float:
    scale = max(1.0, abs(v1) / 10.0, abs(v2) / 10.0)
    return max(0.0, 1.0 - abs(v1 - v2) / scale)


def _same_direction(op1: str, op2: str) -> Optional[bool]:
    if op1 in _GT and op2 in _GT:
        return True
    if op1 in _LT and op2 in _LT:
        return True
    if op1 in _GT and op2 in _LT:
        return False
    if op1 in _LT and op2 in _GT:
        return False
    if op1 == op2:
        return True
    return False


def _is_singleton(verdict: LeafVerdict) -> bool:
    t = verdict.trigger
    if t is None or len(t.intervals) != 1:
        return False
    lo, _, hi, _ = t.intervals[0]
    return lo is not None and hi is not None and lo == hi


def region_similarity(v1: LeafVerdict, v2: LeafVerdict) -> Tuple[float, str]:
    """两个叶子在触发取值域上的相似度及依据。"""
    l1, l2 = v1.leaf, v2.leaf
    if l1.metric != l2.metric:
        return 0.0, "指标不同"
    if v1.trigger is None or v2.trigger is None:
        # 指标缺失 / 聚合不可推导：只能按方向与阈值接近度保守打分
        direction = _same_direction(l1.op, l2.op)
        prox = _threshold_proximity(l1.value, l2.value)
        if direction is True:
            return 0.5 + 0.5 * prox, "取值域未知，按同方向阈值接近度估算"
        if direction is False:
            return 0.5 * prox, "取值域未知，方向相反"
        return 0.4 + 0.3 * prox, "取值域未知，比较符不一致"

    # 等值条件（连续域上的单点）单独处理
    if _is_singleton(v1) or _is_singleton(v2):
        if _is_singleton(v1) and _is_singleton(v2):
            same = l1.value == l2.value
            return (1.0 if same else 0.0), (
                f"等值触发域同为单点 {l1.value:g}" if same else
                f"等值触发域单点不同（{l1.value:g} vs {l2.value:g}）"
            )
        return 0.0, "一个为等值单点、另一个为区间"

    if v1.trigger.is_empty() and v2.trigger.is_empty():
        return 1.0, "两者触发域均为空（同为不可触发条件）"
    if v1.trigger.is_empty() or v2.trigger.is_empty():
        return 0.0, "其中一个触发域为空"

    jac = v1.trigger.jaccard(v2.trigger)
    if jac is not None:
        return jac, f"触发取值域 Jaccard={jac:.2f}"

    # 无界域退化打分
    direction = _same_direction(l1.op, l2.op)
    prox = _threshold_proximity(l1.value, l2.value)
    if direction is True:
        return 0.5 + 0.5 * prox, f"无界域：同方向，阈值接近度={prox:.2f}"
    if direction is False:
        return 0.5 * prox, f"无界域：方向相反（{l1.op}{l1.value:g} vs {l2.op}{l2.value:g}）"
    return 0.4 + 0.3 * prox, "无界域：比较符不一致"


def _window_similarity(w1: Optional[float], w2: Optional[float]) -> float:
    if w1 is None or w2 is None:
        return 0.3 if w1 != w2 else 0.5
    if w1 <= 0 or w2 <= 0:
        return 0.0 if w1 != w2 else 0.5
    return min(w1, w2) / max(w1, w2)


def _op_similarity(op1: str, op2: str) -> float:
    if op1 == op2:
        return 1.0
    direction = _same_direction(op1, op2)
    return 0.8 if direction is True else (0.2 if direction is False else 0.5)


def _for_similarity(f1: Optional[float], f2: Optional[float]) -> float:
    if f1 == f2:
        return 1.0
    if f1 is None or f2 is None:
        return 0.7
    if f1 <= 0 or f2 <= 0:
        return 0.0
    return min(f1, f2) / max(f1, f2)


def leaf_similarity(
    v1: LeafVerdict, v2: LeafVerdict, metrics: Dict[str, Metric]
) -> Tuple[float, List[str]]:
    l1, l2 = v1.leaf, v2.leaf
    if l1.metric != l2.metric:
        return 0.0, [f"指标不同（{l1.metric} vs {l2.metric}）"]
    region_s, region_basis = region_similarity(v1, v2)
    win_s = _window_similarity(l1.window_seconds, l2.window_seconds)
    agg_s = 1.0 if l1.agg == l2.agg else 0.5
    op_s = _op_similarity(l1.op, l2.op)
    for_s = _for_similarity(l1.for_seconds, l2.for_seconds)
    other_s = (agg_s + op_s + for_s) / 3.0
    score = 0.75 * region_s + 0.15 * win_s + 0.10 * other_s
    caps: List[str] = []
    if l1.agg != l2.agg:
        score = min(score, 0.85)
        caps.append(f"聚合方式不同（{l1.agg} vs {l2.agg}），语义不同，封顶 0.85")
    if l1.for_seconds != l2.for_seconds:
        score = min(score, 0.85)
        caps.append("持续时长 for 不同，触发语义不同，封顶 0.85")
    basis = caps + [
        region_basis,
        f"窗口接近度={win_s:.2f}（{l1.window_raw} vs {l2.window_raw}）",
        f"聚合/比较符/持续时长相似度={other_s:.2f}"
        f"（agg {l1.agg}/{l2.agg}，op {l1.op}/{l2.op}）",
        f"加权相似度 0.75×域+0.15×窗口+0.10×其他 = {score:.3f}",
    ]
    return score, basis


# ---------------- 组合树相似度 ----------------

def _collect_leaves(cond: Cond) -> List[Cond]:
    if cond.kind == "leaf":
        return [cond]
    out: List[Cond] = []
    for ch in cond.children:
        out.extend(_collect_leaves(ch))
    return out


def tree_similarity(
    rule_a: Rule, rule_b: Rule, metrics: Dict[str, Metric],
    cache: Dict[str, LeafVerdict],
) -> Tuple[float, List[str]]:
    """结构一致后按相同指标贪心配对叶子，得分除以叶子总数（惩罚缺项）。"""
    ca, cb = to_nnf(rule_a.cond), to_nnf(rule_b.cond)
    if ca.kind != cb.kind:
        return 0.0, ["条件结构不同（单条件 vs 组合条件）"]
    if ca.kind == "leaf":
        key_a = leaf_key(ca.leaf, metrics.get(ca.leaf.metric))
        key_b = leaf_key(cb.leaf, metrics.get(cb.leaf.metric))
        va = cache.setdefault(key_a, analyze_leaf(ca.leaf, metrics))
        vb = cache.setdefault(key_b, analyze_leaf(cb.leaf, metrics))
        return leaf_similarity(va, vb, metrics)

    leaves_a = _collect_leaves(ca)
    leaves_b = _collect_leaves(cb)
    used_b: set = set()
    total = 0.0
    basis: List[str] = []
    for la in leaves_a:
        best_score, best_j, best_basis = 0.0, None, []
        for j, lb in enumerate(leaves_b):
            if j in used_b:
                continue
            if la.leaf.metric != lb.leaf.metric:
                continue
            key_a = leaf_key(la.leaf, metrics.get(la.leaf.metric))
            key_b = leaf_key(lb.leaf, metrics.get(lb.leaf.metric))
            va = cache.setdefault(key_a, analyze_leaf(la.leaf, metrics))
            vb = cache.setdefault(key_b, analyze_leaf(lb.leaf, metrics))
            score, b = leaf_similarity(va, vb, metrics)
            if score > best_score:
                best_score, best_j, best_basis = score, j, b
        if best_j is not None:
            used_b.add(best_j)
            total += best_score
            basis.append(
                f"{la.leaf.metric}: {best_score:.3f}（{'; '.join(best_basis[:1])}）"
            )
        else:
            basis.append(f"{la.leaf.metric}: 在另一规则中无对应条件")
    denom = max(len(leaves_a), len(leaves_b))
    score = total / denom if denom else 0.0
    basis.append(f"{len(used_b)}/{denom} 个叶子配对，综合相似度={score:.3f}")
    return score, basis


# ---------------- 聚类 ----------------

def find_duplicates(rules: List[Rule], metrics: Dict[str, Metric]) -> List[DuplicateGroup]:
    signatures = {rule.id: _canonical_tree(rule.cond, metrics) for rule in rules}
    statuses = {rule.id: analyze_rule(rule, metrics)["status"] for rule in rules}
    cache: Dict[str, LeafVerdict] = {}
    parent = {rule.id: rule.id for rule in rules}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pair_details: List[PairDetail] = []
    edges: List[Tuple[str, str, float, str, List[str]]] = []

    for i, a in enumerate(rules):
        for b in rules[i + 1:]:
            equivalent = signatures[a.id] == signatures[b.id]
            # 两条规则都恒不触发 / 都恒触发时不再算重复：
            # 触发域同为空或同为全没有比较意义，且已分别报告。
            if not equivalent and statuses[a.id] in (NEVER, ALWAYS) and statuses[a.id] == statuses[b.id]:
                continue
            basis: List[str]
            if equivalent:
                score = 1.0
                basis = [f"规范化条件签名一致：{signatures[a.id]}"]
                kind = EQUIVALENT
            else:
                score, basis = tree_similarity(a, b, metrics, cache)
                kind = OVERLAPPING if score >= SIM_THRESHOLD else None
            if kind:
                edges.append((a.id, b.id, score, kind, basis))
                pair_details.append(
                    PairDetail(a.id, b.id, kind, score, basis)
                )
                ra, rb = find(a.id), find(b.id)
                if ra != rb:
                    parent[ra] = rb

    groups: Dict[str, List[str]] = {}
    for rule in rules:
        root = find(rule.id)
        groups.setdefault(root, []).append(rule.id)

    result: List[DuplicateGroup] = []
    for _root, ids in groups.items():
        if len(ids) < 2:
            continue
        related = [p for p in pair_details if p.rule_a in ids and p.rule_b in ids]
        kind = (
            EQUIVALENT
            if all(p.kind == EQUIVALENT for p in related)
            else OVERLAPPING
        )
        result.append(
            DuplicateGroup(
                rule_ids=sorted(ids),
                kind=kind,
                max_similarity=max(p.similarity for p in related),
                pairs=sorted(related, key=lambda p: -p.similarity),
            )
        )
    result.sort(key=lambda g: -g.max_similarity)
    return result
