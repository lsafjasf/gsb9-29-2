"""Self-tests for the errreg framework.

Run from the repository root:
    python3 -m selftest.test_errreg        # or: python3 -m unittest
"""

from __future__ import annotations

import json
import random
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from errreg.core import (Tolerances, DistSummary, PASS, FAIL, ERROR,
                         check_deterministic_error, check_randomized_error)
from errreg.runner import Case, Runner
from errreg.baseline import load_baseline
from errreg.report import write_reports
from examples.problems import (good_cases, degraded_cases,
                               make_sqrt2_newton)


class FrameworkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runner = Runner(self.tmp.name)

    def _case_named(self, cases, name):
        return next(c for c in cases if c.name == name)

    def _build(self, case):
        return self.runner.build_baseline(case)

    # -- scenario: identical results --------------------------------------
    def test_identical_results_pass(self):
        case = self._case_named(good_cases(), "sqrt2_newton_exact")
        self._build(case)
        result = self.runner.check(case)
        self.assertEqual(result.status, PASS, result.error)
        self.assertTrue(all(c.passed for c in result.checks))

    def test_deterministic_with_noise_passes(self):
        case = self._case_named(good_cases(), "sqrt2_newton")
        self._build(case)
        result = self.runner.check(case)
        self.assertEqual(result.status, PASS, result.error)

    # -- scenario: error degradation --------------------------------------
    def test_error_degradation_fails_with_details(self):
        good = self._case_named(good_cases(), "sqrt2_newton")
        self._build(good)
        bad = self._case_named(degraded_cases(), "sqrt2_newton_early_stop")
        result = self.runner.check(bad)
        self.assertEqual(result.status, FAIL)
        failing = [c for c in result.checks
                   if not c.passed and c.level == "fail"]
        self.assertTrue(any("abs_error" in c.metric for c in failing))
        self.assertIn("limit=", failing[0].detail)
        self.assertIn("z=", failing[0].detail)

    def test_randomized_degradation_fails(self):
        good = self._case_named(good_cases(), "pi_monte_carlo")
        self._build(good)
        bad = self._case_named(degraded_cases(), "pi_monte_carlo_biased")
        result = self.runner.check(bad)
        self.assertEqual(result.status, FAIL)
        failing = [c for c in result.checks if not c.passed]
        self.assertTrue(any("t=" in c.detail for c in failing))

    def test_randomized_within_noise_passes(self):
        case = self._case_named(good_cases(), "pi_monte_carlo")
        self._build(case)
        result = self.runner.check(case)
        self.assertEqual(result.status, PASS, result.error)

    # -- scenario: very few samples ---------------------------------------
    def test_few_sample_baseline_passes_with_warning(self):
        case = self._case_named(good_cases(), "sqrt2_newton_sparse")
        baseline = self._build(case)
        self.assertLess(baseline.abs_error.n, self.runner.tol.min_samples)
        result = self.runner.check(case)
        self.assertEqual(result.status, PASS, result.error)
        self.assertTrue(any("sample" in w for w in result.warnings))

    # -- scenario: iteration inflation ------------------------------------
    def test_iteration_inflation_fails(self):
        good = self._case_named(good_cases(), "sqrt2_newton")
        self._build(good)
        bad = self._case_named(degraded_cases(), "sqrt2_newton_extra_iters")
        result = self.runner.check(bad)
        self.assertEqual(result.status, FAIL)
        iter_check = next(c for c in result.checks
                          if c.metric == "iterations")
        self.assertFalse(iter_check.passed)
        # accuracy itself did not degrade: only the iteration guard fires
        abs_check = next(c for c in result.checks
                         if c.metric.startswith("abs_error"))
        self.assertTrue(abs_check.passed)

    # -- reference drift ---------------------------------------------------
    def test_reference_drift_detected(self):
        case = self._case_named(good_cases(), "sqrt2_newton_exact")
        self._build(case)
        drifted = Case(
            name="sqrt2_newton_exact",
            kind="deterministic",
            reference_fn=lambda: Decimal("1.5"),
            trial_fn=make_sqrt2_newton(),
            check_trials=2,
        )
        result = self.runner.check(drifted)
        self.assertEqual(result.status, ERROR)
        self.assertIn("reference", result.error)

    def test_missing_baseline_is_error(self):
        case = self._case_named(good_cases(), "sqrt2_newton_exact")
        result = self.runner.check(case)
        self.assertEqual(result.status, ERROR)

    # -- storage -----------------------------------------------------------
    def test_baseline_roundtrip(self):
        case = self._case_named(good_cases(), "sqrt2_newton_exact")
        baseline = self._build(case)
        loaded = load_baseline(self.tmp.name, case.name)
        self.assertEqual(loaded.to_dict(), baseline.to_dict())

    # -- report ------------------------------------------------------------
    def test_report_written(self):
        for case in good_cases():
            self._build(case)
        results = [self.runner.check(c)
                   for c in good_cases() + degraded_cases()]
        out_dir = Path(self.tmp.name) / "reports"
        out_dir.mkdir()
        paths = write_reports(results, out_dir, label="t")
        md = paths["markdown"].read_text(encoding="utf-8")
        self.assertIn("Overall: FAIL", md)  # degraded cases must fail
        self.assertIn("sqrt2_newton_early_stop", md)
        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["failed"], len(degraded_cases()))
        self.assertEqual(data["summary"]["passed"], len(good_cases()))


class StatisticsTest(unittest.TestCase):
    """False-positive behaviour of the statistical checks themselves."""

    def test_deterministic_false_positive_rate_bounded(self):
        tol = Tolerances()
        rng = random.Random(7)
        mu, sigma = 1e-12, 2e-13
        base = DistSummary(mean=mu, std=sigma, n=12,
                           min=mu - sigma, max=mu + sigma)
        n = 400
        fp = sum(0 if check_deterministic_error(
            rng.gauss(mu, sigma), base, tol, Decimal("1")).passed else 1
            for _ in range(n))
        # theoretical rate ~3e-5; allow generous slack, never flaky
        self.assertLessEqual(fp / n, 0.02)

    def test_randomized_false_positive_rate_at_boundary(self):
        tol = Tolerances()
        rng = random.Random(11)
        mu, sigma, n = 3.2e-3, 4.0e-3, 30
        delta = tol.delta_rel * mu
        base = DistSummary(mean=mu, std=sigma, n=n,
                           min=0.0, max=mu + 2 * sigma)
        reps = 600
        fp = 0
        for _ in range(reps):
            new = DistSummary.from_samples(
                rng.gauss(mu + delta, sigma) for _ in range(n))
            if not check_randomized_error(new, base, tol,
                                          Decimal("1")).passed:
                fp += 1
        # at the boundary the rate should saturate near alpha=0.01;
        # assert it neither vanishes nor explodes
        self.assertLessEqual(fp / reps, 0.05)

    def test_large_degradation_always_detected(self):
        tol = Tolerances()
        rng = random.Random(13)
        mu, sigma, n = 3.2e-3, 4.0e-3, 30
        base = DistSummary(mean=mu, std=sigma, n=n,
                           min=0.0, max=mu + 2 * sigma)
        for _ in range(50):
            new = DistSummary.from_samples(
                rng.gauss(mu * 4.0, sigma) for _ in range(n))
            self.assertFalse(
                check_randomized_error(new, base, tol, Decimal("1")).passed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
