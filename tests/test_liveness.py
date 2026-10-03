"""可触发性推导测试：单规则、组合条件、缺失指标、阈值边界、窗口。"""
import unittest

from alerts_lint.lint import lint_rules
from alerts_lint.liveness import ALWAYS, NEVER, UNKNOWN, VARIABLE, analyze_rule
from alerts_lint.model import Rule, load_metrics, load_rules

METRICS = load_metrics([
    {"name": "cpu",    "min": 0, "max": 100, "every": "60s", "retention": "90d"},
    {"name": "ratio",  "min": 0, "max": 1,   "every": "60s", "retention": "30d"},
    {"name": "ticks",  "min": 0, "max": 10,  "integer": True, "every": "60s", "retention": "30d"},
    {"name": "latency", "min": 0, "max": None, "every": "60s", "retention": "30d"},
    {"name": "nodes",  "min": 0, "max": 1,   "integer": True, "every": "60s", "retention": "30d"},
])


def status(rule_dict):
    rule = Rule.from_dict(rule_dict)
    return analyze_rule(rule, METRICS)["status"]


def leaf(metric, op, value, agg="avg", window="5m", **extra):
    d = {"metric": metric, "agg": agg, "window": window,
         "condition": {"op": op, "value": value}}
    d.update(extra)
    return d


class TestSingleRules(unittest.TestCase):
    def test_never_gt_above_max(self):
        self.assertEqual(status(leaf("cpu", ">", 150)), NEVER)

    def test_never_lt_below_min(self):
        self.assertEqual(status(leaf("ratio", "<", 0)), NEVER)

    def test_always_ge_min(self):
        self.assertEqual(status(leaf("cpu", ">=", 0)), ALWAYS)

    def test_always_gt_below_min(self):
        self.assertEqual(status(leaf("cpu", ">", -1)), ALWAYS)

    def test_always_lt_above_max(self):
        self.assertEqual(status(leaf("cpu", "<", 150)), ALWAYS)

    def test_normal_high_threshold(self):
        self.assertEqual(status(leaf("cpu", ">", 90)), VARIABLE)

    def test_normal_low_threshold(self):
        # 低阈值合法规则：0.01 是合理告警，不得判成恒触发
        self.assertEqual(status(leaf("ratio", ">", 0.01)), VARIABLE)

    def test_normal_ratio_one_boundary(self):
        # > 1 对 [0,1] 不可触发；但 > 1 与 >= 1 必须区分
        self.assertEqual(status(leaf("ratio", ">", 1)), NEVER)
        self.assertEqual(status(leaf("ratio", ">=", 1)), VARIABLE)

    def test_normal_eq_inside(self):
        self.assertEqual(status(leaf("cpu", "==", 50)), VARIABLE)

    def test_eq_at_max(self):
        # 连续域上 == max 仍可触发（单个取值）
        self.assertEqual(status(leaf("cpu", "==", 100)), VARIABLE)

    def test_neq_inside_variable(self):
        self.assertEqual(status(leaf("cpu", "!=", 50)), VARIABLE)

    def test_neq_outside_always(self):
        self.assertEqual(status(leaf("cpu", "!=", 150)), ALWAYS)

    def test_unbounded_high_threshold_variable(self):
        # 无界指标再高的阈值也不能断言不可触发
        self.assertEqual(status(leaf("latency", ">", 10 ** 9)), VARIABLE)

    def test_unbounded_ge_zero_always(self):
        self.assertEqual(status(leaf("latency", ">=", 0)), ALWAYS)


class TestIntegerBoundary(unittest.TestCase):
    def test_integer_gt_at_max_never(self):
        self.assertEqual(status(leaf("ticks", ">", 10)), NEVER)

    def test_integer_ge_at_max_variable(self):
        self.assertEqual(status(leaf("ticks", ">=", 10)), VARIABLE)

    def test_integer_lt_at_min_never(self):
        self.assertEqual(status(leaf("ticks", "<", 0)), NEVER)

    def test_integer_le_at_min_variable(self):
        self.assertEqual(status(leaf("ticks", "<=", 0)), VARIABLE)

    def test_integer_gt_just_under_max_variable(self):
        # > 9 在整数域 [0,10] 上还有取值 10
        self.assertEqual(status(leaf("ticks", ">", 9)), VARIABLE)


class TestAggregationDomains(unittest.TestCase):
    def test_sum_bounded_never(self):
        # 5 个样本，每个 [0,100]，总和 [0,500]；> 600 不可触发
        self.assertEqual(status(leaf("cpu", ">", 600, agg="sum")), NEVER)

    def test_sum_bounded_variable(self):
        self.assertEqual(status(leaf("cpu", ">", 400, agg="sum")), VARIABLE)

    def test_sum_bounded_always(self):
        # 总和最小值为 0；< 0 恒假（即 < 0 条件永远不触发）
        self.assertEqual(status(leaf("cpu", "<", 0, agg="sum")), NEVER)
        # 总和 <= 500 恒真
        self.assertEqual(status(leaf("cpu", "<=", 500, agg="sum")), ALWAYS)

    def test_sum_unbounded_inconclusive(self):
        self.assertEqual(status(leaf("latency", ">", 10 ** 12, agg="sum")), UNKNOWN)

    def test_count_high_never(self):
        # 5m/60s = 5 个样本，count > 10 不可能
        self.assertEqual(status(leaf("cpu", ">", 10, agg="count")), NEVER)

    def test_count_at_boundary_variable(self):
        self.assertEqual(status(leaf("cpu", ">=", 5, agg="count")), VARIABLE)

    def test_rate_nonnegative_ge_zero_always(self):
        self.assertEqual(status(leaf("cpu", ">=", 0, agg="rate")), ALWAYS)

    def test_rate_high_variable(self):
        self.assertEqual(status(leaf("cpu", ">", 100000, agg="rate")), VARIABLE)

    def test_delta_range_never(self):
        # cpu ∈ [0,100]，delta ∈ [-100,100]；> 101 不可触发
        self.assertEqual(status(leaf("cpu", ">", 101, agg="delta")), NEVER)


class TestComposite(unittest.TestCase):
    def all_of(self, *pairs):
        return {"id": "c", "condition": {"all": [
            {"metric": m, "agg": "avg", "window": "5m", "op": op, "value": v}
            for m, op, v in pairs]}}

    def any_of(self, *pairs):
        return {"id": "c", "condition": {"any": [
            {"metric": m, "agg": "avg", "window": "5m", "op": op, "value": v}
            for m, op, v in pairs]}}

    def test_and_contradiction(self):
        self.assertEqual(analyze_rule(
            Rule.from_dict(self.all_of(("cpu", ">", 90), ("cpu", "<", 80))),
            METRICS)["status"], NEVER)

    def test_and_adjacent_boundary_fine(self):
        # > 80 且 < 90 是合法窗口
        self.assertEqual(analyze_rule(
            Rule.from_dict(self.all_of(("cpu", ">", 80), ("cpu", "<", 90))),
            METRICS)["status"], VARIABLE)

    def test_and_different_agg_no_false_contradiction(self):
        # avg>90 且 max<80 是不同的量，不得判为矛盾
        rule = {"id": "c", "condition": {"all": [
            {"metric": "cpu", "agg": "avg", "window": "5m", "op": ">", "value": 90},
            {"metric": "cpu", "agg": "max", "window": "5m", "op": "<", "value": 80},
        ]}}
        self.assertEqual(status(rule), VARIABLE)

    def test_or_tautology(self):
        r = self.any_of(("cpu", ">", 90), ("cpu", "<=", 90))
        self.assertEqual(analyze_rule(Rule.from_dict(r), METRICS)["status"], ALWAYS)

    def test_or_normal(self):
        r = self.any_of(("cpu", ">", 90), ("ratio", ">", 0.8))
        self.assertEqual(analyze_rule(Rule.from_dict(r), METRICS)["status"], VARIABLE)

    def test_or_all_impossible(self):
        r = self.any_of(("cpu", ">", 150), ("cpu", "<", -10))
        self.assertEqual(analyze_rule(Rule.from_dict(r), METRICS)["status"], NEVER)

    def test_not_flips_always(self):
        # not(cpu > 150) ⟺ cpu <= 150，恒真
        rule = {"id": "c", "condition": {"not":
            {"metric": "cpu", "agg": "avg", "window": "5m", "op": ">", "value": 150}}}
        self.assertEqual(status(rule), ALWAYS)

    def test_not_flips_never(self):
        # not(cpu >= 0) ⟺ cpu < 0，恒假
        rule = {"id": "c", "condition": {"not":
            {"metric": "cpu", "agg": "avg", "window": "5m", "op": ">=", "value": 0}}}
        self.assertEqual(status(rule), NEVER)

    def test_nested_composite(self):
        # (cpu>90 AND ratio<0.5) OR cpu<0  —— 左支可真可假，整体正常
        rule = {"id": "c", "condition": {"any": [
            {"all": [
                {"metric": "cpu", "agg": "avg", "window": "5m", "op": ">", "value": 90},
                {"metric": "ratio", "agg": "avg", "window": "5m", "op": "<", "value": 0.5},
            ]},
            {"metric": "cpu", "agg": "avg", "window": "5m", "op": "<", "value": 0},
        ]}}
        self.assertEqual(status(rule), VARIABLE)


class TestMissingMetric(unittest.TestCase):
    def test_missing_is_inconclusive_not_error(self):
        rule = Rule.from_dict(leaf("unknown_metric", ">", 10))
        result = analyze_rule(rule, METRICS)
        self.assertEqual(result["status"], UNKNOWN)
        self.assertTrue(result["unknown"])

    def test_missing_produces_warning(self):
        report = lint_rules([Rule.from_dict(leaf("unknown_metric", ">", 10))], METRICS)
        kinds = {f["kind"] for f in report["findings"]}
        self.assertIn("missing_metric", kinds)
        self.assertEqual(report["summary"]["errors"], 0)

    def test_metric_present_but_undecidable(self):
        self.assertEqual(status(leaf("latency", ">", 1, agg="sum")), UNKNOWN)


class TestWindowChecks(unittest.TestCase):
    def test_window_exceeds_retention_never(self):
        self.assertEqual(status(leaf("ratio", ">", 0.5, window="31d")), NEVER)

    def test_window_equals_retention_allowed(self):
        self.assertEqual(status(leaf("ratio", ">", 0.5, window="30d")), VARIABLE)

    def test_long_window_within_retention_is_fine(self):
        # 合法长窗口不得误报
        self.assertEqual(status(leaf("cpu", ">", 90, window="60d")), VARIABLE)

    def test_zero_window_invalid(self):
        self.assertEqual(status(leaf("cpu", ">", 90, window="0m")), NEVER)

    def test_garbage_window_invalid(self):
        self.assertEqual(status(leaf("cpu", ">", 90, window="soon")), NEVER)

    def test_window_shorter_than_sample_info_only(self):
        # 窗口短于采样间隔：提示但不改变可触发性结论
        rule = Rule.from_dict(leaf("cpu", ">", 90, window="10s"))
        result = analyze_rule(rule, METRICS)
        self.assertEqual(result["status"], VARIABLE)
        self.assertTrue(result["window_notes"])


if __name__ == "__main__":
    unittest.main()
