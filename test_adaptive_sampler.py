"""Self-tests for AdaptiveSampler. Stdlib only: python3 test_adaptive_sampler.py"""

import random
import unittest

from adaptive_sampler import AdaptiveSampler

TARGET = 500.0          # target reported events/sec
RATE_TOL = 0.05         # 5% rate tolerance (on smoothed / averaged rate)
AVG_TOL = 0.03          # 3% tolerance on mean reported rate
DIST_TOL = 0.02         # 2 percentage points max distribution deviation
MAX_CONVERGE_WINDOWS = 5

SERVICES = [("web", 0.40), ("api", 0.35), ("worker", 0.15), ("batch", 0.10)]
REGIONS = [("us", 0.50), ("eu", 0.30), ("ap", 0.20)]


def pick(rng, table):
    r = rng.random()
    acc = 0.0
    for value, prob in table:
        acc += prob
        if r < acc:
            return value
    return table[-1][0]


def make_event(rng, important_frac=0.05, slow_frac=0.02):
    return {
        "error": rng.random() < important_frac,
        "duration_ms": 2000.0 if rng.random() < slow_frac else rng.random() * 100.0,
        "dims": {"service": pick(rng, SERVICES), "region": pick(rng, REGIONS)},
    }


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make_sampler(clock, seed=42, **kw):
    kw.setdefault("target_rate", TARGET)
    kw.setdefault("window_sec", 1.0)
    return AdaptiveSampler(rng=random.Random(seed), time_fn=clock, **kw)


def drive(sampler, clock, rate_per_sec, seconds, rng, important_frac=0.05,
          slow_frac=0.02, all_important=False):
    """Drive traffic; returns list of kept-per-second (one entry per window)."""
    kept_per_window = []
    for _ in range(seconds):
        kept = 0
        if rate_per_sec > 0:
            step = 1.0 / rate_per_sec
            for _ in range(rate_per_sec):
                clock.t += step
                if all_important:
                    event = {"error": True, "duration_ms": 1.0,
                             "dims": {"service": "web", "region": "us"}}
                else:
                    event = make_event(rng, important_frac, slow_frac)
                if sampler.should_sample(event):
                    kept += 1
        else:
            clock.t += 1.0
        kept_per_window.append(kept)
    return kept_per_window


def convergence_window(rates, target, tol=RATE_TOL, smooth=3):
    """First window from which the `smooth`-window moving average stays within tol.

    Per-window reported counts carry binomial noise, so convergence is judged
    on the moving average, matching how an ops dashboard would be read.
    """
    n = len(rates)
    if n < smooth:
        return None
    avgs = [sum(rates[j:j + smooth]) / smooth for j in range(n - smooth + 1)]
    for i in range(len(avgs)):
        if all(abs(a - target) / target <= tol for a in avgs[i:]):
            return i
    return None


def mean_deviation(rates, target):
    return abs(sum(rates) / len(rates) - target) / target


class TestRateTracking(unittest.TestCase):
    def test_steady_state_rate(self):
        clock = Clock()
        s = make_sampler(clock)
        rng = random.Random(1)
        drive(s, clock, 2000, 10, rng)  # warm-up
        rates = drive(s, clock, 2000, 20, rng)
        self.assertLessEqual(mean_deviation(rates, TARGET), AVG_TOL,
                             "mean rate deviates > 3%%: %s" % rates)
        conv = convergence_window(rates, TARGET)
        self.assertEqual(conv, 0, "steady state not stable: %s" % rates)

    def test_surge_10x(self):
        clock = Clock()
        s = make_sampler(clock)
        rng = random.Random(2)
        # important_frac kept low so the important stream alone (3% of 10k
        # ~= 300/s) stays below target and the controller has budget to tune.
        drive(s, clock, 1000, 10, rng, important_frac=0.01)   # steady 1000/s
        rates = drive(s, clock, 10000, 20, rng, important_frac=0.01)  # 10x surge
        conv = convergence_window(rates, TARGET)
        self.assertIsNotNone(conv, "never converged after 10x surge: %s" % rates)
        self.assertLessEqual(conv, MAX_CONVERGE_WINDOWS,
                             "converged in %s windows (> %s)" % (conv, MAX_CONVERGE_WINDOWS))
        self.assertLessEqual(mean_deviation(rates[conv + 2:], TARGET), AVG_TOL)
        # Important events still 100% kept during the surge.
        self.assertEqual(s.total_important_incoming, s.total_important_kept)

    def test_traffic_drop(self):
        clock = Clock()
        s = make_sampler(clock)
        rng = random.Random(3)
        drive(s, clock, 10000, 10, rng)
        rates = drive(s, clock, 200, 15, rng)   # drop below target
        # Incoming (200/s) < target (500/s): everything normal must be kept,
        # i.e. reported ~= incoming and p -> 1.0.
        tail = rates[-5:]
        for r in tail:
            self.assertGreaterEqual(r, 0.95 * 200,
                                    "under-sampling after drop: %s" % r)
        self.assertGreater(s.probability, 0.95)

    def test_all_important_exceeds_target(self):
        clock = Clock()
        s = make_sampler(clock)
        rates = drive(s, clock, 2000, 10, random.Random(4), all_important=True)
        # Important events are never dropped, even at 4x target.
        for r in rates:
            self.assertEqual(r, 2000)
        self.assertEqual(s.total_important_kept, s.total_important_incoming)
        self.assertEqual(s.total_kept, s.total_incoming)

    def test_no_traffic(self):
        clock = Clock()
        s = make_sampler(clock)
        rng = random.Random(5)
        drive(s, clock, 2000, 5, rng)
        p_before = s.probability
        rates = drive(s, clock, 0, 30, rng)     # silence for 30s
        self.assertEqual(rates, [0] * 30)
        # Sampler recovers immediately when traffic resumes.
        rates = drive(s, clock, 2000, 15, rng)
        conv = convergence_window(rates, TARGET)
        self.assertIsNotNone(conv, "did not recover after silence: %s" % rates)
        self.assertLessEqual(conv, MAX_CONVERGE_WINDOWS)
        self.assertGreaterEqual(s.probability, 0.0)
        self.assertLessEqual(s.probability, 1.0)
        self.assertGreaterEqual(p_before, 0.0)


class TestImportancePriority(unittest.TestCase):
    def test_important_never_dropped_under_pressure(self):
        clock = Clock()
        s = make_sampler(clock, target_rate=100.0)
        rng = random.Random(6)
        drive(s, clock, 5000, 10, rng, important_frac=0.10)
        self.assertEqual(s.total_important_incoming, s.total_important_kept)
        self.assertGreater(s.total_important_incoming, 0)

    def test_important_dims(self):
        clock = Clock()
        s = make_sampler(clock, important_dims={"service": {"payments"}},
                         target_rate=10.0)
        ev = {"error": False, "duration_ms": 1.0, "dims": {"service": "payments"}}
        self.assertTrue(s.is_important(ev))
        ev2 = {"error": False, "duration_ms": 1.0, "dims": {"service": "web"}}
        self.assertFalse(s.is_important(ev2))

    def test_slow_request_important(self):
        clock = Clock()
        s = make_sampler(clock, slow_threshold_ms=500.0)
        self.assertTrue(s.is_important({"error": False, "duration_ms": 500.0}))
        self.assertFalse(s.is_important({"error": False, "duration_ms": 499.9}))


class TestRepresentativeness(unittest.TestCase):
    def test_dimension_distribution_preserved(self):
        clock = Clock()
        s = make_sampler(clock)
        rng = random.Random(7)
        drive(s, clock, 2000, 5, rng)  # warm-up

        pop = {"service": {}, "region": {}}
        sam = {"service": {}, "region": {}}
        pop_normal = 0
        sam_normal = 0
        seconds = 100
        rate = 2000
        for _ in range(seconds):
            for _ in range(rate):
                clock.t += 1.0 / rate
                ev = make_event(rng, important_frac=0.05)
                important = s.is_important(ev)
                kept = s.should_sample(ev)
                if important:
                    continue  # important stratum is 100% kept by design
                pop_normal += 1
                for dim in pop:
                    v = ev["dims"][dim]
                    pop[dim][v] = pop[dim].get(v, 0) + 1
                if kept:
                    sam_normal += 1
                    for dim in sam:
                        v = ev["dims"][dim]
                        sam[dim][v] = sam[dim].get(v, 0) + 1

        self.assertGreater(sam_normal, 1000, "too few samples to be meaningful")
        for dim in pop:
            for value, count in pop[dim].items():
                p_pop = count / pop_normal
                p_sam = sam[dim].get(value, 0) / sam_normal
                self.assertLessEqual(
                    abs(p_pop - p_sam), DIST_TOL,
                    "dim %s=%s: pop %.4f vs sampled %.4f" % (dim, value, p_pop, p_sam))


class TestValidation(unittest.TestCase):
    def test_invalid_args(self):
        with self.assertRaises(ValueError):
            AdaptiveSampler(target_rate=0)
        with self.assertRaises(ValueError):
            AdaptiveSampler(target_rate=1, window_sec=0)
        with self.assertRaises(ValueError):
            AdaptiveSampler(target_rate=1, smoothing=0)
        with self.assertRaises(ValueError):
            AdaptiveSampler(target_rate=1, min_probability=1.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
