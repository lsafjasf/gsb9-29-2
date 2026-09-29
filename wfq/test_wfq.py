"""Self-tests for the WFQ simulator. Run: python3 -m unittest -v"""

import unittest

from wfq import WFQSimulator, QueueConfigError


def backlog(sim, qname, n, size=1.0, t0=0.0):
    for i in range(n):
        sim.submit(qname, size, t0 + i * 1e-9)  # ~simultaneous burst


class TestBasicFairness(unittest.TestCase):
    def test_single_queue_gets_full_bandwidth(self):
        sim = WFQSimulator(rate=10.0)
        sim.add_queue("only", weight=5)
        backlog(sim, "only", 100)
        stats = sim.run()
        self.assertAlmostEqual(stats.share("only"), 1.0)
        # link rate 10, 100 bytes -> finishes at t=10
        self.assertAlmostEqual(sim.finish_time, 10.0)
        self.assertAlmostEqual(stats.throughput("only", 0.0, 10.0), 10.0)

    def test_weighted_shares_two_backlogged_queues(self):
        sim = WFQSimulator(rate=1.0)
        sim.add_queue("a", weight=3)
        sim.add_queue("b", weight=1)
        backlog(sim, "a", 4000)
        backlog(sim, "b", 4000)
        stats = sim.run()
        # a drains at ~5333, b at 8000: measure while BOTH are backlogged.
        self.assertAlmostEqual(stats.share("a", 0.0, 4000.0), 0.75, delta=0.02)
        self.assertAlmostEqual(stats.share("b", 0.0, 4000.0), 0.25, delta=0.02)

    def test_weighted_shares_many_queues(self):
        weights = {"q1": 1, "q2": 2, "q3": 3, "q4": 4}
        sim = WFQSimulator(rate=100.0)
        for name, w in weights.items():
            sim.add_queue(name, weight=w)
            backlog(sim, name, 5000)
        stats = sim.run()
        total_w = sum(weights.values())
        # q4 (weight 4) drains first, at ~125: measure while all backlogged.
        for name, w in weights.items():
            self.assertAlmostEqual(stats.share(name, 0.0, 120.0),
                                   w / total_w, delta=0.02)

    def test_negative_weight_rejected(self):
        sim = WFQSimulator()
        with self.assertRaises(QueueConfigError):
            sim.add_queue("bad", weight=-1)

    def test_unknown_queue_rejected(self):
        sim = WFQSimulator()
        sim.add_queue("a", 1)
        with self.assertRaises(QueueConfigError):
            sim.submit("nope", 1.0)


class TestZeroWeight(unittest.TestCase):
    def test_zero_weight_alone_gets_full_link(self):
        sim = WFQSimulator(rate=10.0)
        sim.add_queue("best-effort", weight=0)
        backlog(sim, "best-effort", 50)
        stats = sim.run()
        self.assertAlmostEqual(stats.share("best-effort"), 1.0)
        self.assertAlmostEqual(sim.finish_time, 5.0)

    def test_zero_weight_yields_to_positive_weight(self):
        sim = WFQSimulator(rate=1.0)
        sim.add_queue("vip", weight=1)
        sim.add_queue("scavenger", weight=0)
        backlog(sim, "vip", 100, t0=0.0)          # busy during t in [0,100]
        backlog(sim, "scavenger", 100, t0=0.0)    # always backlogged
        stats = sim.run()
        # While vip is active the zero-weight queue gets (almost) nothing.
        self.assertLess(stats.bytes_served("scavenger", 0.0, 100.0), 1.0)
        # After vip drains, the scavenger uses the full link.
        self.assertAlmostEqual(stats.throughput("scavenger", 100.0, 200.0),
                               1.0, delta=0.02)


class TestIdleQueueReclaim(unittest.TestCase):
    def test_idle_share_instantly_reclaimed(self):
        # A idle for the first half; B always backlogged; equal weights.
        sim = WFQSimulator(rate=1.0)
        sim.add_queue("a", weight=1)
        sim.add_queue("b", weight=1)
        backlog(sim, "b", 1000, t0=0.0)
        backlog(sim, "a", 500, t0=500.0)  # A only shows up at t=500
        stats = sim.run()
        # While A is idle, B must use (essentially) the whole link.
        self.assertGreaterEqual(stats.throughput("b", 0.0, 500.0), 0.999)
        self.assertEqual(stats.bytes_served("a", 0.0, 500.0), 0.0)
        # After A arrives, shares converge to 50/50.
        self.assertAlmostEqual(stats.share("a", 510.0, 1000.0), 0.5, delta=0.02)
        self.assertAlmostEqual(stats.share("b", 510.0, 1000.0), 0.5, delta=0.02)

    def test_returning_queue_cannot_monopolize(self):
        # A leaves and returns; it must not hoard credit while away.
        sim = WFQSimulator(rate=1.0)
        sim.add_queue("a", weight=1)
        sim.add_queue("b", weight=1)
        backlog(sim, "a", 100, t0=0.0)      # A active [0,100]
        backlog(sim, "b", 2000, t0=0.0)     # B always backlogged
        backlog(sim, "a", 100, t0=1000.0)   # A returns at t=1000
        stats = sim.run()
        # In the window right after A returns, B still gets its fair half.
        self.assertAlmostEqual(stats.share("b", 1000.0, 1200.0), 0.5,
                               delta=0.05)


class TestExtremeWeightsAndBursts(unittest.TestCase):
    def test_no_starvation_extreme_weights_burst(self):
        # 1:100 weight ratio, everything arrives as one giant burst at t=0.
        sim = WFQSimulator(rate=1.0)
        sim.add_queue("thin", weight=1)
        sim.add_queue("fat", weight=100)
        backlog(sim, "thin", 50)
        backlog(sim, "fat", 20000)
        stats = sim.run()
        # All thin packets are eventually served.
        self.assertEqual(len(sim.service_starts["thin"]), 50)
        # Service-gap bound: a queue with weight w among total W is
        # revisited within ~W/w packet service times. Here 101.
        bound = (1 + 100) / 1 + 1.0
        self.assertLessEqual(stats.max_service_gap("thin"), bound)
        # Shares track the weights while both are backlogged
        # (thin drains at ~5050).
        self.assertAlmostEqual(stats.share("thin", 0.0, 5000.0), 1 / 101,
                               delta=0.005)

    def test_max_wait_reported_and_bounded(self):
        sim = WFQSimulator(rate=1.0)
        sim.add_queue("a", weight=1)
        sim.add_queue("b", weight=3)
        backlog(sim, "a", 100)
        backlog(sim, "b", 100)
        stats = sim.run()
        # a's fair rate is 1/4 byte/unit; its i-th packet starts by ~4i.
        self.assertLessEqual(stats.max_wait("a"), 4 * 100 + 4.0)
        self.assertGreater(stats.max_wait("a"), 0.0)


class TestChurn(unittest.TestCase):
    def test_queues_frequently_joining_and_leaving(self):
        # a and b alternately burst 4 packets in 30-unit cycles; c is
        # always backlogged. Equal weights.
        sim = WFQSimulator(rate=1.0)
        for name in ("a", "b", "c"):
            sim.add_queue(name, weight=1)
        for k in range(10):
            base = 30.0 * k
            backlog(sim, "a", 4, t0=base)          # drains by base+8
            backlog(sim, "b", 4, t0=base + 10.0)   # drains by base+18
        backlog(sim, "c", 500, t0=0.0)
        stats = sim.run()
        # While a is backlogged it splits the link 50/50 with c.
        self.assertAlmostEqual(stats.share("a", 0.0, 8.0), 0.5, delta=0.06)
        self.assertAlmostEqual(stats.share("b", 10.0, 18.0), 0.5, delta=0.06)
        # While both are idle, c instantly uses the full link.
        self.assertGreaterEqual(stats.throughput("c", 20.0, 30.0), 0.999)
        # Nobody waits absurdly: every a/b packet is served within ~8
        # time units of its burst arrival.
        self.assertLessEqual(stats.max_wait("a"), 9.0)
        self.assertLessEqual(stats.max_wait("b"), 9.0)


class TestAllBurst(unittest.TestCase):
    def test_simultaneous_burst_all_queues(self):
        weights = {"x": 1, "y": 2, "z": 5}
        sim = WFQSimulator(rate=10.0)
        for name, w in weights.items():
            sim.add_queue(name, weight=w)
            backlog(sim, name, 2000, t0=0.0)
        stats = sim.run()
        total_w = sum(weights.values())
        # z (weight 5) drains first, at ~320: measure while all backlogged.
        for name, w in weights.items():
            self.assertAlmostEqual(stats.share(name, 0.0, 300.0),
                                   w / total_w, delta=0.02)
        # Work conserving: total bytes / rate == busy time.
        self.assertAlmostEqual(sim.finish_time, 3 * 2000 / 10.0)


if __name__ == "__main__":
    unittest.main()
