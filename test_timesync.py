"""Regression tests.

The first two tests reproduce the reported defects against the naive
baseline (drift on long runs, clock steps being silently averaged in) and
show that the fixed Synchronizer does not have them.  The remaining tests
cover the required edge cases: asymmetric RTT, zero delay, constant offset,
extreme jitter, step-vs-jitter discrimination and long unavailability.
"""

import unittest

from simulation import LocalClock, Network, Scenario, run
from timesync import Synchronizer
from timesync_naive import NaiveSynchronizer


def make_scenario(**kw):
    clock = LocalClock(offset=kw.pop("offset", 0.0), skew=kw.pop("skew", 0.0))
    for t_step, delta in kw.pop("steps", ()):
        clock.add_step(t_step, delta)
    net = Network(**kw)
    return Scenario(clock, net)


def residual(sync, scenario, t):
    return sync.offset_estimate() - scenario.true_offset(t)


class ReproductionTests(unittest.TestCase):
    def test_long_run_drift(self):
        """100 ppm skew over 1 h: naive drifts away, fixed stays tight."""
        scenario = make_scenario(offset=0.3, skew=100e-6, seed=1,
                                 fwd=0.02, bwd=0.02, jitter=1e-3)
        naive = NaiveSynchronizer()
        fixed = Synchronizer()
        t_end = 3600.0
        run(scenario, naive, t_end)
        # Deterministic replay for the fixed sync (same seed -> same delays).
        scenario = make_scenario(offset=0.3, skew=100e-6, seed=1,
                                 fwd=0.02, bwd=0.02, jitter=1e-3)
        run(scenario, fixed, t_end)

        naive_res = abs(naive.offset_estimate() - scenario.true_offset(t_end))
        fixed_res = abs(residual(fixed, scenario, t_end))
        # Bug reproduced: cumulative mean lags the linearly moving offset.
        self.assertGreater(naive_res, 0.1)
        # Fixed: skew is tracked, residual stays small after 1 h.
        self.assertLess(fixed_res, 0.02)
        self.assertAlmostEqual(fixed.skew(), -100e-6, delta=20e-6)

    def test_clock_step_not_eaten(self):
        """+500 ms local-clock step: naive absorbs it, fixed reconverges."""
        steps = [(600.0, 0.5)]
        scenario = make_scenario(offset=0.1, steps=steps, seed=2,
                                 fwd=0.02, bwd=0.02, jitter=1e-3)
        naive = NaiveSynchronizer()
        run(scenario, naive, 1200.0)
        naive_res = abs(naive.offset_estimate()
                        - scenario.true_offset(1200.0))
        # Bug reproduced: the step is averaged in, estimate stays wrong.
        self.assertGreater(naive_res, 0.1)

        scenario = make_scenario(offset=0.1, steps=steps, seed=2,
                                 fwd=0.02, bwd=0.02, jitter=1e-3)
        fixed = Synchronizer()
        run(scenario, fixed, 1200.0)
        # The step is detected, logged and followed by re-convergence.
        self.assertEqual(len(fixed.step_events), 1)
        event = fixed.step_events[0]
        self.assertGreaterEqual(event.t, 600.0)
        self.assertLess(event.t, 620.0)
        self.assertAlmostEqual(event.magnitude, -0.5, delta=0.05)
        self.assertLess(abs(residual(fixed, scenario, 1200.0)), 0.02)


class EdgeCaseTests(unittest.TestCase):
    def test_asymmetric_rtt(self):
        """Constant 50/10 ms asymmetry biases any 4-timestamp estimator by
        half the asymmetry (20 ms); the fix must stay within that bound and
        be no worse than the naive mean."""
        scenario = make_scenario(offset=0.0, seed=3,
                                 fwd=0.05, bwd=0.01, jitter=2e-3)
        fixed = Synchronizer()
        run(scenario, fixed, 600.0)
        res = residual(fixed, scenario, 600.0)
        self.assertLessEqual(abs(res), 0.020 + 5e-3)

        scenario = make_scenario(offset=0.0, seed=3,
                                 fwd=0.05, bwd=0.01, jitter=2e-3)
        naive = NaiveSynchronizer()
        run(scenario, naive, 600.0)
        naive_res = naive.offset_estimate() - scenario.true_offset(600.0)
        self.assertLessEqual(abs(res), abs(naive_res) + 5e-3)

    def test_zero_delay(self):
        scenario = make_scenario(offset=0.7, seed=4,
                                 fwd=0.0, bwd=0.0, jitter=0.0)
        fixed = Synchronizer()
        run(scenario, fixed, 30.0)
        self.assertLess(abs(residual(fixed, scenario, 30.0)), 1e-9)

    def test_constant_offset(self):
        scenario = make_scenario(offset=-2.5, seed=5,
                                 fwd=0.01, bwd=0.01, jitter=5e-4)
        fixed = Synchronizer()
        run(scenario, fixed, 120.0)
        self.assertLess(abs(residual(fixed, scenario, 120.0)), 2e-3)
        self.assertEqual(fixed.stats()["filtered"], 0)

    def test_extreme_jitter(self):
        """+-50 ms jitter plus 5% 500 ms spikes: filtered, no false steps."""
        scenario = make_scenario(offset=0.05, skew=20e-6, seed=6,
                                 fwd=0.02, bwd=0.02, jitter=0.05,
                                 spike_prob=0.05, spike_mag=0.5)
        fixed = Synchronizer()
        run(scenario, fixed, 1800.0)
        stats = fixed.stats()
        self.assertGreater(stats["filter_ratio"], 0.03)
        self.assertLess(stats["filter_ratio"], 0.5)
        self.assertEqual(len(fixed.step_events), 0)
        self.assertLess(abs(residual(fixed, scenario, 1800.0)), 0.05)

    def test_step_distinguished_from_jitter(self):
        # Isolated 1 s spikes must NOT be reported as clock steps.
        scenario = make_scenario(offset=0.0, seed=7,
                                 fwd=0.02, bwd=0.02, jitter=1e-3,
                                 spike_prob=0.02, spike_mag=1.0)
        fixed = Synchronizer()
        run(scenario, fixed, 900.0)
        self.assertEqual(len(fixed.step_events), 0)

        # A real persistent step must be reported exactly once.
        scenario = make_scenario(offset=0.0, seed=7, steps=[(300.0, 0.3)],
                                 fwd=0.02, bwd=0.02, jitter=1e-3,
                                 spike_prob=0.02, spike_mag=1.0)
        fixed = Synchronizer()
        run(scenario, fixed, 900.0)
        self.assertEqual(len(fixed.step_events), 1)
        self.assertAlmostEqual(fixed.step_events[0].magnitude, -0.3, delta=0.05)

    def test_long_unavailability(self):
        """10 min outage: holdover keeps predicting via the skew model."""
        scenario = make_scenario(offset=0.2, skew=50e-6, seed=8,
                                 fwd=0.02, bwd=0.02, jitter=2e-3,
                                 outages=[(300.0, 900.0)])
        fixed = Synchronizer(holdover_after=60.0)
        statuses = {}

        def snapshot(t, sync, scn):
            if abs(t - 200.0) < 1e-9:
                statuses["mid_run"] = sync.status(scn.clock.read(t))
            if abs(t - 899.0) < 1e-9:
                statuses["in_outage"] = sync.status(scn.clock.read(t))

        run(scenario, fixed, 1200.0, on_row=snapshot)

        self.assertGreater(fixed.failed_polls, 500)
        self.assertEqual(statuses["mid_run"], "synced")
        # During the outage the synchronizer reports holdover, not synced.
        self.assertEqual(statuses["in_outage"], "holdover")
        # The skew model keeps the prediction usable through the outage:
        # at the end of the outage the drift is ~30 ms and must be tracked.
        est = fixed.offset_estimate(899.0)
        self.assertIsNotNone(est)
        self.assertLess(abs(est - scenario.true_offset(899.0)), 0.05)
        # After recovery it reconverges and reports synced again.
        self.assertEqual(fixed.status(1200.0), "synced")
        self.assertLess(abs(residual(fixed, scenario, 1200.0)), 0.02)

    def test_filter_contract(self):
        self.assertIn("MAD", Synchronizer.FILTER_CRITERION)
        scenario = make_scenario(offset=0.0, seed=9, jitter=1e-3)
        fixed = Synchronizer()
        run(scenario, fixed, 100.0)
        stats = fixed.stats()
        for key in ("total", "filtered", "filter_ratio", "failed_polls",
                    "skew", "residual_sigma", "steps"):
            self.assertIn(key, stats)
        self.assertGreaterEqual(stats["filter_ratio"], 0.0)
        self.assertLessEqual(stats["filter_ratio"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
