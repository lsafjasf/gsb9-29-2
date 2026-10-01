"""覆盖率统计与提示。"""
from __future__ import annotations

from collections import Counter

from . import harness
from .generator import EDGE_KINDS, EDGE_LABELS, STRATEGIES


class Coverage:
    def __init__(self):
        self.total = 0
        self.strategy_counts = Counter()
        self.edge_counts = Counter()
        self.edge_unsupported = Counter()
        self.errors_a = Counter()
        self.errors_b = Counter()
        self.categories = Counter()
        self.max_depth = 0
        self.max_size = 0

    def record_skip(self, kind):
        if kind is not None:
            self.edge_unsupported[kind] += 1

    def record(self, case, outcome_a, outcome_b, category):
        self.total += 1
        if case.tag.startswith("edge:"):
            self.edge_counts[case.meta["edge"]] += 1
        else:
            self.strategy_counts[case.tag] += 1
        self.categories[category] += 1
        if outcome_a.error:
            self.errors_a[outcome_a.error] += 1
        if outcome_b.error:
            self.errors_b[outcome_b.error] += 1
        self.max_depth = max(self.max_depth, case.meta.get("depth", 0))
        self.max_size = max(self.max_size, case.meta.get("size", 0))

    def hints(self):
        tips = []
        for kind in EDGE_KINDS:
            if self.edge_unsupported.get(kind):
                tips.append(
                    f"未覆盖「{EDGE_LABELS[kind]}」：spec.edge_case('{kind}') "
                    "返回了 None，请在协议适配器中实现该边界情形"
                )
            elif not self.edge_counts.get(kind):
                tips.append(f"未覆盖「{EDGE_LABELS[kind]}」：请增大 --cases")
        for strategy in STRATEGIES:
            if not self.strategy_counts.get(strategy):
                tips.append(f"生成策略「{strategy}」未命中：请增大 --cases")
        if not self.errors_a and not self.errors_b:
            tips.append(
                "两个实现从未报错：非法输入覆盖不足，请检查 illegal_sequence "
                "策略与 edge_case 的实现"
            )
        if self.categories.get(harness.MATCH) == self.total:
            tips.append(
                "未发现任何差异：两实现行为一致；若这不符合预期，"
                "请增大 --cases 或检查 parse_a/parse_b 是否误接为同一实现"
            )
        if len(self.errors_a) + len(self.errors_b) <= 2:
            tips.append(
                "观测到的错误类别较少：可在适配器中补充更多非法序列类型，"
                "以覆盖各实现的全部错误分支"
            )
        return tips

    def to_json(self):
        return {
            "total": self.total,
            "strategy_counts": dict(self.strategy_counts),
            "edge_counts": dict(self.edge_counts),
            "edge_unsupported": dict(self.edge_unsupported),
            "error_categories_a": dict(self.errors_a),
            "error_categories_b": dict(self.errors_b),
            "case_categories": dict(self.categories),
            "max_tree_depth_generated": self.max_depth,
            "max_size_generated": self.max_size,
        }
