"""边界用例集回归：examples/boundary_cases.json 中每条用例的期望结论必须成立。"""
import json
import os
import unittest

from alerts_lint.lint import lint_rules
from alerts_lint.model import Rule, load_metrics

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(HERE, "examples")


def load_cases():
    with open(os.path.join(EXAMPLES, "boundary_cases.json"), encoding="utf-8") as fh:
        return json.load(fh)["cases"]


def load_example_metrics():
    with open(os.path.join(EXAMPLES, "metrics.json"), encoding="utf-8") as fh:
        return load_metrics(json.load(fh)["metrics"])


class TestBoundaryCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.metrics = load_example_metrics()
        cls.cases = load_cases()

    def test_dataset_shape(self):
        self.assertGreaterEqual(len(self.cases), 25)
        ids = [c["id"] for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)), "用例 id 必须唯一")

    def test_each_case(self):
        for case in self.cases:
            with self.subTest(case=case["id"]):
                raw = dict(case["rule"])
                raw.setdefault("id", case["id"])
                report = lint_rules([Rule.from_dict(raw)], self.metrics)
                actual = report["status_by_rule"][case["id"]]["status"]
                self.assertEqual(
                    actual, case["expect"],
                    f"{case['id']}（{case['note']}）期望 {case['expect']}，实际 {actual}",
                )
                expected_finding = case.get("finding")
                if expected_finding:
                    kinds = {f["kind"] for f in report["findings"]}
                    self.assertIn(expected_finding, kinds,
                                  f"{case['id']} 应产生 {expected_finding} 发现")
                if case["expect"] == "may_trigger":
                    # 合法用例必须零 error/warning 级发现（误报控制的核心断言）；
                    # 已声明的 info 级提示（如窗口短于采样间隔）允许存在
                    bad = [
                        f for f in report["findings"]
                        if f["severity"] in ("error", "warning")
                        and f["kind"] != case.get("finding")
                    ]
                    self.assertEqual(
                        bad, [],
                        f"{case['id']}（{case['note']}）不应产生 error/warning 发现，"
                        f"实际: {[f['kind'] for f in bad]}",
                    )
                elif case["expect"] in ("never_triggers", "always_triggers"):
                    self.assertTrue(
                        report["findings"],
                        f"{case['id']} 应产生问题发现",
                    )


if __name__ == "__main__":
    unittest.main()
