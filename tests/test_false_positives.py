"""误报对照：合法规则集（含低阈值、长窗口、无界高阈值等）必须零问题。"""
import json
import os
import unittest

from alerts_lint.lint import lint_rules
from alerts_lint.liveness import ALWAYS, NEVER, UNKNOWN, VARIABLE, analyze_rule
from alerts_lint.model import Rule, load_metrics, load_rules

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METRICS = load_metrics(json.load(open(os.path.join(HERE, "examples", "metrics.json")))["metrics"])
LEGIT = load_rules(json.load(open(os.path.join(HERE, "examples", "rules_legit.json")))["rules"])


class TestLegitSetClean(unittest.TestCase):
    def setUp(self):
        self.report = lint_rules(LEGIT, METRICS)

    def test_all_variable(self):
        bad = {
            rid: st["label"]
            for rid, st in self.report["status_by_rule"].items()
            if st["status"] != VARIABLE
        }
        self.assertEqual(bad, {}, f"合法规则被误判: {bad}")

    def test_no_findings(self):
        # info 级窗口提示也不允许出现在合法集里
        self.assertEqual(self.report["findings"], [])

    def test_no_duplicate_groups(self):
        self.assertEqual(self.report["summary"]["duplicate_groups"], 0)

    def test_exit_code_zero_semantics(self):
        self.assertEqual(self.report["summary"]["errors"], 0)
        self.assertEqual(self.report["summary"]["warnings"], 0)


class TestFPGuardrailsExplicit(unittest.TestCase):
    """典型易误报场景逐条钉死。"""

    def check(self, rule_dict, expected=VARIABLE):
        self.assertEqual(analyze_rule(Rule.from_dict(rule_dict), METRICS)["status"], expected)

    def test_low_threshold_ratio(self):
        self.check({"id": "fp1", "metric": "error_rate", "window": "5m",
                    "condition": {"op": ">", "value": 0.001}})

    def test_low_threshold_cpu(self):
        self.check({"id": "fp2", "metric": "cpu_usage", "window": "5m",
                    "condition": {"op": ">", "value": 1}})

    def test_long_window_within_retention(self):
        self.check({"id": "fp3", "metric": "cpu_usage", "window": "90d",
                    "condition": {"op": ">", "value": 90}})

    def test_window_equals_retention(self):
        self.check({"id": "fp4", "metric": "availability", "window": "30d",
                    "condition": {"op": "<", "value": 0.999}})

    def test_huge_threshold_unbounded_metric(self):
        self.check({"id": "fp5", "metric": "latency_ms", "window": "5m",
                    "condition": {"op": ">", "value": 10 ** 12}})

    def test_equality_on_continuous_max(self):
        self.check({"id": "fp6", "metric": "cpu_usage", "window": "5m",
                    "condition": {"op": "==", "value": 100}})

    def test_ge_at_min_is_variable_for_nonnegative_unbounded(self):
        # latency_ms >= 0 是恒真（正确报告），但 > 0 只是合法低阈值
        self.check({"id": "fp7", "metric": "latency_ms", "window": "5m",
                    "condition": {"op": ">", "value": 0}})
        self.check({"id": "fp7b", "metric": "latency_ms", "window": "5m",
                    "condition": {"op": ">=", "value": 0}}, ALWAYS)

    def test_max_agg_legit(self):
        self.check({"id": "fp8", "metric": "cpu_usage", "agg": "max", "window": "5m",
                    "condition": {"op": ">", "value": 90}})

    def test_p99_agg_legit(self):
        self.check({"id": "fp9", "metric": "latency_ms", "agg": "p99", "window": "5m",
                    "condition": {"op": ">", "value": 500}})

    def test_rate_agg_legit(self):
        self.check({"id": "fp10", "metric": "http_5xx_count", "agg": "rate", "window": "5m",
                    "condition": {"op": ">", "value": 10}})

    def test_sum_legit_bounded(self):
        self.check({"id": "fp11", "metric": "cpu_usage", "agg": "sum", "window": "5m",
                    "condition": {"op": ">", "value": 400}})

    def test_for_duration_legit(self):
        self.check({"id": "fp12", "metric": "cpu_usage", "window": "5m", "for": "2h",
                    "condition": {"op": ">", "value": 90}})

    def test_not_of_normal_rule(self):
        # not(cpu>90) 是合法反向规则（cpu<=90 告警），可真可假
        self.check({"id": "fp13", "condition": {"not":
            {"metric": "cpu_usage", "window": "5m", "op": ">", "value": 90}}})

    def test_or_across_metrics(self):
        self.check({"id": "fp14", "condition": {"any": [
            {"metric": "cpu_usage", "window": "5m", "op": ">", "value": 90},
            {"metric": "error_rate", "window": "5m", "op": ">", "value": 0.8},
        ]}})


if __name__ == "__main__":
    unittest.main()
