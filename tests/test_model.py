"""时长解析与规则/指标解析测试。"""
import unittest

from alerts_lint.model import (
    Metric,
    Rule,
    parse_duration,
    parse_condition,
)


class TestParseDuration(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_duration("5m"), 300)
        self.assertEqual(parse_duration("1h"), 3600)
        self.assertEqual(parse_duration("2d"), 172800)
        self.assertEqual(parse_duration("1w"), 604800)
        self.assertEqual(parse_duration("30s"), 30)
        self.assertEqual(parse_duration("500ms"), 0.5)

    def test_combined(self):
        self.assertEqual(parse_duration("1h30m"), 5400)
        self.assertEqual(parse_duration("5.5m"), 330.0)

    def test_numeric(self):
        self.assertEqual(parse_duration(90), 90.0)
        self.assertEqual(parse_duration("90"), 90.0)
        self.assertIsNone(parse_duration(None))

    def test_invalid(self):
        for bad in ("", "soon", "-5m", "5x", "m5", True):
            self.assertIsNone(parse_duration(bad), bad)


class TestRuleParsing(unittest.TestCase):
    def test_basic_leaf(self):
        rule = Rule.from_dict({
            "id": "r", "metric": "cpu", "window": "5m",
            "condition": {"op": ">", "value": 90},
        })
        self.assertEqual(rule.cond.kind, "leaf")
        self.assertEqual(rule.cond.leaf.metric, "cpu")
        self.assertEqual(rule.cond.leaf.window_seconds, 300)

    def test_name_fallback(self):
        rule = Rule.from_dict({"name": "named", "metric": "m",
                               "condition": {"op": ">", "value": 1}})
        self.assertEqual(rule.id, "named")

    def test_inherited_fields(self):
        cond = parse_condition({"all": [{"op": ">", "value": 90}]},
                               {"metric": "cpu", "window": "10m"})
        self.assertEqual(cond.children[0].leaf.metric, "cpu")
        self.assertEqual(cond.children[0].leaf.window_seconds, 600)

    def test_bad_op_raises(self):
        with self.assertRaises(ValueError):
            Rule.from_dict({"id": "r", "metric": "cpu",
                            "condition": {"op": "==", "value": "hot"}})

    def test_missing_metric_raises(self):
        with self.assertRaises(ValueError):
            parse_condition({"op": ">", "value": 1}, {})

    def test_metric_from_dict(self):
        metric = Metric.from_dict({"name": "m", "min": 0, "max": 1,
                                   "integer": True, "every": "1m",
                                   "retention": "7d"})
        self.assertTrue(metric.integer)
        self.assertEqual(metric.every_seconds, 60)
        self.assertEqual(metric.retention_seconds, 604800)


if __name__ == "__main__":
    unittest.main()
