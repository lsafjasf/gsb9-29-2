"""重复检测测试：等价、近重复、近邻不合并、边界相似度、误报护栏。"""
import unittest

from alerts_lint.duplicates import (
    EQUIVALENT,
    OVERLAPPING,
    find_duplicates,
)
from alerts_lint.model import Rule, load_metrics

METRICS = load_metrics([
    {"name": "cpu", "min": 0, "max": 100, "every": "60s", "retention": "90d"},
    {"name": "latency", "min": 0, "max": None, "every": "60s", "retention": "30d"},
    {"name": "ticks", "min": 0, "max": 10, "integer": True, "every": "60s", "retention": "30d"},
])


def rule(rule_id, metric, op, value, agg="avg", window="5m", for_=None):
    d = {"id": rule_id, "metric": metric, "agg": agg, "window": window,
         "condition": {"op": op, "value": value}}
    if for_ is not None:
        d["for"] = for_
    return Rule.from_dict(d)


def group_map(groups):
    return {tuple(g.rule_ids): g for g in groups}


class TestEquivalence(unittest.TestCase):
    def test_identical_rules_equivalent(self):
        groups = find_duplicates(
            [rule("a", "cpu", ">", 90), rule("b", "cpu", ">", 90)], METRICS)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].kind, EQUIVALENT)
        self.assertEqual(groups[0].max_similarity, 1.0)

    def test_integer_canonicalization(self):
        # 整数域 ticks > 9 等价于 >= 10
        groups = find_duplicates(
            [rule("a", "ticks", ">", 9), rule("b", "ticks", ">=", 10)], METRICS)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].kind, EQUIVALENT)

    def test_reordered_composite_equivalent(self):
        ra = Rule.from_dict({"id": "a", "condition": {"all": [
            {"metric": "cpu", "window": "5m", "op": ">", "value": 90},
            {"metric": "ticks", "window": "5m", "op": "<", "value": 5},
        ]}})
        rb = Rule.from_dict({"id": "b", "condition": {"all": [
            {"metric": "ticks", "window": "5m", "op": "<", "value": 5},
            {"metric": "cpu", "window": "5m", "op": ">", "value": 90},
        ]}})
        groups = find_duplicates([ra, rb], METRICS)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].kind, EQUIVALENT)

    def test_different_threshold_not_equivalent(self):
        groups = find_duplicates(
            [rule("a", "cpu", ">", 90), rule("b", "cpu", ">", 91)], METRICS)
        self.assertEqual([g.kind for g in groups if g.kind == EQUIVALENT], [])


class TestHighOverlap(unittest.TestCase):
    def test_near_window_grouped(self):
        groups = find_duplicates(
            [rule("a", "cpu", ">", 92, window="5m"),
             rule("b", "cpu", ">", 92, window="6m")], METRICS)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].kind, OVERLAPPING)

    def test_three_times_window_not_grouped(self):
        # 5m vs 15m：窗口差异大，即使阈值相同也不合并
        groups = find_duplicates(
            [rule("a", "cpu", ">", 90, window="5m"),
             rule("b", "cpu", ">", 90, window="15m")], METRICS)
        self.assertEqual(groups, [])

    def test_same_threshold_unbounded_grouped(self):
        # 无界指标：同方向同阈值，5m vs 6m 仍应判重叠
        groups = find_duplicates(
            [rule("a", "latency", ">", 500, window="5m"),
             rule("b", "latency", ">", 500, window="6m")], METRICS)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].kind, OVERLAPPING)

    def test_opposite_directions_not_grouped(self):
        groups = find_duplicates(
            [rule("a", "cpu", ">", 80), rule("b", "cpu", "<", 80)], METRICS)
        self.assertEqual(groups, [])

    def test_unbounded_opposite_directions_not_grouped(self):
        groups = find_duplicates(
            [rule("a", "latency", ">", 500), rule("b", "latency", "<", 500)], METRICS)
        self.assertEqual(groups, [])

    def test_different_metric_not_grouped(self):
        groups = find_duplicates(
            [rule("a", "cpu", ">", 90), rule("b", "latency", ">", 90)], METRICS)
        self.assertEqual(groups, [])


class TestSemanticGuardrails(unittest.TestCase):
    def test_different_agg_not_grouped(self):
        # avg 持续高 vs max 瞬时高，触发域数值上相同但语义不同 → 封顶不合并
        groups = find_duplicates(
            [rule("a", "cpu", ">", 90, agg="avg"),
             rule("b", "cpu", ">", 90, agg="max")], METRICS)
        self.assertEqual(groups, [])

    def test_different_for_not_grouped(self):
        # 立即触发 vs 持续 2h 才触发，不合并
        groups = find_duplicates(
            [rule("a", "cpu", ">", 90),
             rule("b", "cpu", ">", 90, for_="2h")], METRICS)
        self.assertEqual(groups, [])

    def test_distant_threshold_bounded_not_grouped(self):
        # 有界指标，[80,100] 与 [90,100] Jaccard=0.5，属不同告警级别
        groups = find_duplicates(
            [rule("a", "cpu", ">", 80), rule("b", "cpu", ">", 90)], METRICS)
        self.assertEqual(groups, [])

    def test_both_never_not_grouped(self):
        # 两条都不可触发（>150、==120）不算重复
        groups = find_duplicates(
            [rule("a", "cpu", ">", 150), rule("b", "cpu", "==", 120)], METRICS)
        self.assertEqual(groups, [])

    def test_both_always_not_grouped(self):
        groups = find_duplicates(
            [rule("a", "cpu", ">=", 0), rule("b", "cpu", "<", 150)], METRICS)
        self.assertEqual(groups, [])

    def test_near_window_threshold_boundary(self):
        # 触发域完全相同时，窗口比 5/6=0.833 → 0.975 判重叠；
        # 窗口比 5/12=0.417 → 0.913 低于 0.92 阈值，不合并
        grouped = find_duplicates(
            [rule("a", "cpu", ">", 95, window="5m"),
             rule("b", "cpu", ">", 95, window="6m")], METRICS)
        self.assertEqual(len(grouped), 1)
        not_grouped = find_duplicates(
            [rule("a", "cpu", ">", 95, window="5m"),
             rule("b", "cpu", ">", 95, window="12m")], METRICS)
        self.assertEqual(not_grouped, [])


if __name__ == "__main__":
    unittest.main()
