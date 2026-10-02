"""Self-tests for the nrt framework (stdlib unittest)."""

import random
import unittest

from nrt import (
    CaseBaseline,
    Tolerances,
    check_case,
    measure,
    summarize,
    t_critical,
)
from nrt.report import render_text, to_json_dict


def det_case(errors, iterations, times, **kw):
    return CaseBaseline(name="case", kind="deterministic", errors=errors,
                        iterations=iterations, times_ms=times, **kw)


class StatsTests(unittest.TestCase):
    def test_t_critical_table_and_tail(self):
        self.assertAlmostEqual(t_critical(1), 63.657)
        self.assertAlmostEqual(t_critical(30), 2.750)
        # large df approaches the normal quantile 2.5758
        self.assertAlmostEqual(t_critical(100_000), 2.5758, places=3)

    def test_summarize(self):
        s = summarize([1.0, 2.0, 3.0])
        self.assertEqual(s.n, 3)
        self.assertAlmostEqual(s.mean, 2.0)
        self.assertAlmostEqual(s.stdev, 1.0)


class ErrorCheckTests(unittest.TestCase):
    def test_identical_results_pass(self):
        base = det_case([1.2e-16] * 3, [6] * 3, [1.0] * 3)
        res = check_case(base, [1.2e-16], [6], [1.1])
        self.assertEqual(res.status, "PASS")

    def test_exact_zero_error_uses_abs_floor(self):
        base = det_case([0.0] * 3, [6] * 3, [1.0] * 3,
                        tolerances={"abs_floor": 1e-15})
        self.assertEqual(check_case(base, [5e-16], [6], [1.0]).status, "PASS")
        self.assertEqual(check_case(base, [1e-12], [6], [1.0]).status, "FAIL")

    def test_error_degradation_fails_with_details(self):
        base = det_case([1e-12] * 3, [40] * 3, [10.0] * 3)
        res = check_case(base, [1e-9], [40], [10.0])
        self.assertEqual(res.status, "FAIL")
        err = res.checks[0]
        self.assertFalse(err.ok)
        self.assertIn("limit", err.summary)
        self.assertGreater(err.data["exceedance"], 0.0)
        self.assertEqual(err.data["mode"], "deterministic")

    def test_tiny_sample_baseline_gets_wide_interval(self):
        # n=3 baseline -> t(2 df) = 9.925: limit ~= 2.25e-3, very wide.
        tiny = CaseBaseline(name="c", kind="randomized",
                            errors=[1.0e-3, 1.1e-3, 1.2e-3],
                            iterations=[10] * 3, times_ms=[1.0] * 3)
        self.assertEqual(check_case(tiny, [1.6e-3], [10], [1.0]).status, "PASS")
        self.assertEqual(check_case(tiny, [3.0e-3], [10], [1.0]).status, "FAIL")

        # Same mean/stdev with n=30 (t=2.75, limit ~= 1.38e-3): the moderate
        # deviation the tiny baseline tolerated is now correctly rejected.
        spread = [1.1e-3 + (1e-4 if i % 2 else -1e-4) for i in range(30)]
        big = CaseBaseline(name="c", kind="randomized", errors=spread,
                           iterations=[10] * 30, times_ms=[1.0] * 30)
        self.assertEqual(check_case(big, [1.6e-3], [10], [1.0]).status, "FAIL")

    def test_randomized_within_noise_passes(self):
        rng = random.Random(0)
        base = CaseBaseline(
            name="c", kind="randomized",
            errors=[rng.gauss(1e-3, 1e-4) for _ in range(30)],
            iterations=[100] * 30, times_ms=[2.0] * 30)
        current = [rng.gauss(1e-3, 1e-4) for _ in range(8)]
        res = check_case(base, current, [100] * 8, [2.0] * 8)
        self.assertEqual(res.status, "PASS")
        self.assertEqual(res.checks[0].data["mode"], "statistical")
        self.assertIsNotNone(res.checks[0].data["z_score"])

    def test_iteration_inflation_fails_even_if_error_improves(self):
        base = det_case([1e-12] * 3, [40, 41, 42], [10.0] * 3)
        res = check_case(base, [1e-14], [60], [10.0])  # error improved!
        self.assertEqual(res.status, "FAIL")
        it = res.checks[1]
        self.assertFalse(it.ok)
        self.assertTrue(it.data.get("accuracy_bought_with_iterations"))

    def test_timing_regression_warns_but_does_not_fail(self):
        base = det_case([1e-12] * 3, [40] * 3, [10.0, 10.5, 9.5])
        res = check_case(base, [1e-12], [40], [30.0])
        self.assertEqual(res.status, "WARN")
        self.assertFalse(res.checks[2].ok)


class ReportTests(unittest.TestCase):
    def test_report_contains_deviation_details(self):
        base = det_case([1e-12] * 3, [40] * 3, [10.0] * 3)
        res = check_case(base, [1e-9], [60], [10.0])
        text = render_text([res], meta={"generated_utc": "2026-10-03T00:00:00+00:00"})
        self.assertIn("[FAIL] case", text)
        self.assertIn("limit", text)
        self.assertIn("exceedance", text)
        self.assertIn("overall: FAIL", text)
        payload = to_json_dict([res])
        self.assertEqual(payload["overall"], "FAIL")
        self.assertEqual(payload["counts"]["FAIL"], 1)
        self.assertEqual(payload["cases"][0]["checks"][0]["name"], "error")


class EndToEndTests(unittest.TestCase):
    def test_stable_problem_passes(self):
        from examples.problems import Sqrt2Newton

        problem = Sqrt2Newton()
        errors, iters, times = measure(problem, 3)
        base = CaseBaseline(name=problem.name, kind=problem.kind, errors=errors,
                            iterations=iters, times_ms=times)
        errors2, iters2, times2 = measure(problem, 1)
        res = check_case(base, errors2, iters2, times2,
                         defaults=Tolerances(time_factor=1e9))
        self.assertEqual(res.status, "PASS")

    def test_degradation_injection_is_caught(self):
        from examples.problems import DegradedProblem, JacobiSolver

        problem = JacobiSolver()
        errors, iters, times = measure(problem, 3)
        base = CaseBaseline(name=problem.name, kind=problem.kind, errors=errors,
                            iterations=iters, times_ms=times)
        degraded = DegradedProblem(problem, perturb=1e-6, iter_factor=1.5)
        errors2, iters2, times2 = measure(degraded, 1)
        res = check_case(base, errors2, iters2, times2,
                         defaults=Tolerances(time_factor=1e9))
        self.assertEqual(res.status, "FAIL")
        failing = {c.name for c in res.checks if not c.ok}
        self.assertIn("error", failing)
        self.assertIn("iterations", failing)


if __name__ == "__main__":
    unittest.main()
