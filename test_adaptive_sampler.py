"""Self-tests for AdaptiveSampler. Stdlib only: python3 -m unittest -v"""

import random
import unittest

from adaptive_sampler import AdaptiveSampler, default_importance
from benchmark import (FakeClock, make_event, run, convergence_time,
                       steady_stats, dist_deviation)

TARGET = 100.0
TOL = 0.05  # 5% rate tolerance


class RateControlTest(unittest.TestCase):
    def _sampler(self, clock, **kw):
        kw.setdefault("key_dimensions", ("service",))
        return AdaptiveSampler(TARGET, clock=clock, **kw)

    def test_steady_rate_within_tolerance(self):
        clock = FakeClock()
        rng = random.Random(11)
        s = self._sampler(clock)
        run(s, clock, 1000, 40, lambda i: make_event(rng, ["web"], [1.0]))
        mean_dev, max_dev = steady_stats(s.history, TARGET, start=10.0)
        self.assertLessEqual(mean_dev, 0.01)
        self.assertLessEqual(max_dev, TOL)
        self.assertLessEqual(convergence_time(s.history, TARGET, 0.0), 3.0)

    def test_burst_10x_converges_and_caps_overshoot(self):
        clock = FakeClock()
        rng = random.Random(12)
        s = self._sampler(clock)
        run(s, clock, 100, 15, lambda i: make_event(rng, ["web"], [1.0]))
        burst_start = clock.t
        run(s, clock, 1000, 30, lambda i: make_event(rng, ["web"], [1.0]))
        self.assertLessEqual(convergence_time(s.history, TARGET, burst_start), 3.0)
        peak = max(h["reported_rate"] for h in s.history
                   if h["end"] >= burst_start)
        self.assertLessEqual(peak, TARGET * s.slack + 1)
        _, max_dev = steady_stats(s.history, TARGET, start=burst_start + 5)
        self.assertLessEqual(max_dev, TOL)

    def test_drop_recovers_rate(self):
        clock = FakeClock()
        rng = random.Random(13)
        s = self._sampler(clock)
        run(s, clock, 1000, 15, lambda i: make_event(rng, ["web"], [1.0]))
        drop_start = clock.t
        run(s, clock, 400, 25, lambda i: make_event(rng, ["web"], [1.0]))
        self.assertLessEqual(convergence_time(s.history, TARGET, drop_start), 3.0)
        _, max_dev = steady_stats(s.history, TARGET, start=drop_start + 5)
        self.assertLessEqual(max_dev, TOL)

    def test_drop_below_target_keeps_everything(self):
        clock = FakeClock()
        rng = random.Random(14)
        s = self._sampler(clock)
        run(s, clock, 1000, 10, lambda i: make_event(rng, ["web"], [1.0]))
        run(s, clock, 40, 5, lambda i: make_event(rng, ["web"], [1.0]))
        # after ~2 windows the controller converges to p=1.0 (keep-all)
        emitted = run(s, clock, 40, 5, lambda i: make_event(rng, ["web"], [1.0]))
        self.assertEqual(emitted, 200)  # input < target: report all
        self.assertEqual(s.p, 1.0)


class ImportanceTest(unittest.TestCase):
    def test_all_important_fully_reported(self):
        clock = FakeClock()
        rng = random.Random(15)
        s = AdaptiveSampler(TARGET, clock=clock)
        total = run(s, clock, 500, 10,
                    lambda i: {**make_event(rng, ["web"], [1.0]), "error": True})
        self.assertEqual(total, 5000)
        self.assertEqual(s.stats()["important_out"], 5000)

    def test_important_priority_under_heavy_load(self):
        clock = FakeClock()
        rng = random.Random(16)
        s = AdaptiveSampler(TARGET, clock=clock,
                            importance_fn=lambda e: default_importance(
                                e, slow_ms=500,
                                dimension_values={"tier": {"gold"}}))
        important_in = [0]

        def gen(i):
            e = make_event(rng, ["web"], [1.0], error_rate=0.05, slow_rate=0.02)
            if i % 50 == 0:
                e["dimensions"]["tier"] = "gold"
            important_in[0] += s._important_fn(e)
            return e

        run(s, clock, 2000, 20, gen)
        st = s.stats()
        self.assertEqual(st["important_out"], important_in[0])  # 100% recall
        self.assertGreater(important_in[0], 0)

    def test_default_importance_rules(self):
        self.assertTrue(default_importance({"error": True}))
        self.assertTrue(default_importance({"latency_ms": 500}))
        self.assertFalse(default_importance({"latency_ms": 499}))
        self.assertTrue(default_importance(
            {"dimensions": {"tier": "gold"}}, dimension_values={"tier": {"gold"}}))
        self.assertFalse(default_importance(
            {"dimensions": {"tier": "free"}}, dimension_values={"tier": {"gold"}}))


class EdgeCaseTest(unittest.TestCase):
    def test_no_traffic_then_resume(self):
        clock = FakeClock()
        rng = random.Random(17)
        s = AdaptiveSampler(TARGET, clock=clock)
        run(s, clock, 1000, 5, lambda i: make_event(rng, ["web"], [1.0]))
        clock.t += 600.0  # long idle: no observe() calls, no spurious output
        out_before = s.total_out
        kept = sum(s.observe(make_event(rng, ["web"], [1.0])) for _ in range(3))
        self.assertEqual(s.total_out - out_before, kept)
        self.assertEqual(kept, 3)  # p resets toward 1.0 after idle

    def test_zero_events_no_history_growth(self):
        clock = FakeClock()
        s = AdaptiveSampler(TARGET, clock=clock)
        clock.t += 100.0
        self.assertEqual(len(s.history), 0)
        self.assertEqual(s.total_out, 0)

    def test_invalid_target_rejected(self):
        with self.assertRaises(ValueError):
            AdaptiveSampler(0)


class DistributionTest(unittest.TestCase):
    def test_dimension_distribution_preserved(self):
        clock = FakeClock()
        rng = random.Random(18)
        services = ["web", "api", "worker", "batch"]
        weights = [0.50, 0.30, 0.15, 0.05]
        s = AdaptiveSampler(200, key_dimensions=("service",), clock=clock)
        run(s, clock, 2000, 30, lambda i: make_event(rng, services, weights))
        st = s.stats()
        _, max_dev, tvd = dist_deviation(st["key_in"], st["key_out"])
        self.assertLessEqual(max_dev, 0.01)   # <= 1 percentage point
        self.assertLessEqual(tvd, 0.01)

    def test_deficit_state_does_not_leak_unseen_keys(self):
        clock = FakeClock()
        rng = random.Random(19)
        s = AdaptiveSampler(TARGET, key_dimensions=("service",), clock=clock)
        run(s, clock, 500, 3, lambda i: make_event(rng, ["a", "b"], [0.5, 0.5]))
        run(s, clock, 500, 3, lambda i: make_event(rng, ["c"], [1.0]))
        self.assertEqual(set(s._deficit), {("c",)})


if __name__ == "__main__":
    unittest.main()
