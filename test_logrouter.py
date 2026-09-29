"""Self-tests for logrouter. Run: python3 -m unittest -v"""

import threading
import time
import unittest

from logrouter import DropPolicy, LogRouter, Rule


class Collector:
    """Thread-safe sink that records every delivered record."""

    def __init__(self):
        self.records = []
        self.lock = threading.Lock()

    def __call__(self, record):
        with self.lock:
            self.records.append(record)

    def __len__(self):
        with self.lock:
            return len(self.records)


class TestMatching(unittest.TestCase):
    def setUp(self):
        self.router = LogRouter()
        self.sink = Collector()
        self.router.add_destination("out", self.sink)

    def tearDown(self):
        self.router.close()

    def test_eq_prefix_regex(self):
        r = self.router
        r.add_rule(Rule("by-level", "level", "eq", "ERROR", ("out",)))
        r.add_rule(Rule("by-prefix", "msg", "prefix", "disk", ("out",)))
        r.add_rule(Rule("by-regex", "msg", "regex", r"code=\d{3}", ("out",)))
        self.assertEqual(r.route({"level": "ERROR", "msg": "x"}), ["out"])
        self.assertEqual(r.route({"level": "INFO", "msg": "disk full"}), ["out"])
        self.assertEqual(r.route({"level": "INFO", "msg": "got code=500"}), ["out"])
        self.assertEqual(r.route({"level": "INFO", "msg": "nothing"}), [])

    def test_missing_field_does_not_match(self):
        self.router.add_rule(Rule("needs-host", "host", "prefix", "web", ("out",)))
        self.assertEqual(self.router.route({"msg": "no host field"}), [])

    def test_invalid_op_rejected(self):
        with self.assertRaises(ValueError):
            Rule("bad", "f", "contains", "x", ("out",))


class TestOverlapAndDedup(unittest.TestCase):
    def test_overlapping_rules_all_fire_and_dest_deduped(self):
        router = LogRouter()
        sink_a, sink_b = Collector(), Collector()
        router.add_destination("a", sink_a)
        router.add_destination("b", sink_b)
        # Both rules match the same record; both point at "a".
        router.add_rule(Rule("r1", "level", "eq", "ERROR", ("a", "b")))
        router.add_rule(Rule("r2", "msg", "prefix", "db", ("a",)))
        targets = router.route({"level": "ERROR", "msg": "db down"})
        router.close()
        # Hit order: r1's destinations first, then r2's new ones.
        self.assertEqual(targets, ["a", "b"])
        # Dedup: "a" matched by both rules but receives the record once.
        self.assertEqual(len(sink_a), 1)
        self.assertEqual(len(sink_b), 1)
        stats = router.stats()
        self.assertEqual(stats.rule_hits, {"r1": 1, "r2": 1})
        self.assertEqual(stats.routed, 1)


class TestNoMatchAndAllMatch(unittest.TestCase):
    def test_no_match_counted_and_not_delivered(self):
        router = LogRouter()
        sink = Collector()
        router.add_destination("out", sink)
        router.add_rule(Rule("only-errors", "level", "eq", "ERROR", ("out",)))
        for _ in range(5):
            self.assertEqual(router.route({"level": "DEBUG"}), [])
        router.close()
        stats = router.stats()
        self.assertEqual(stats.unmatched, 5)
        self.assertEqual(stats.routed, 0)
        self.assertEqual(len(sink), 0)
        self.assertEqual(stats.destinations["out"]["enqueued"], 0)

    def test_all_match_multiple_destinations(self):
        router = LogRouter()
        sinks = [Collector() for _ in range(3)]
        for i, sink in enumerate(sinks):
            router.add_destination(f"d{i}", sink)
        router.add_rule(Rule("all", "level", "regex", r".*", ("d0", "d1", "d2")))
        for n in range(10):
            router.route({"level": "INFO", "n": n})
        router.close()
        for sink in sinks:
            self.assertEqual(len(sink), 10)
        stats = router.stats()
        self.assertEqual(stats.routed, 10)
        self.assertEqual(stats.unmatched, 0)


class TestIsolation(unittest.TestCase):
    def test_blocked_destination_does_not_slow_others(self):
        router = LogRouter()
        fast = Collector()
        release = threading.Event()

        def blocking_sender(record):
            release.wait(timeout=10)  # stuck until test releases it

        router.add_destination("slow", blocking_sender, queue_size=4)
        router.add_destination("fast", fast, queue_size=1000)
        router.add_rule(Rule("all", "msg", "regex", r".*", ("slow", "fast")))

        n = 200
        start = time.monotonic()
        for i in range(n):
            router.route({"msg": f"log-{i}"})
        enqueue_elapsed = time.monotonic() - start

        # Fast destination keeps up while slow one is fully blocked.
        deadline = time.monotonic() + 5
        while len(fast) < n and time.monotonic() < deadline:
            time.sleep(0.005)
        release.set()
        router.close(timeout=2)

        self.assertEqual(len(fast), n)
        self.assertLess(enqueue_elapsed, 2.0, "route() blocked on slow destination")
        stats = router.stats()
        slow = stats.destinations["slow"]
        # Buffer bound: queue_size=4, worker holds 1 -> at most 5 in flight,
        # everything else dropped and counted.
        self.assertLessEqual(slow["enqueued"], 5)
        self.assertEqual(slow["enqueued"] + slow["dropped"], n)
        self.assertGreater(slow["dropped"], 0)
        self.assertEqual(stats.destinations["fast"]["dropped"], 0)

    def test_drop_oldest_policy(self):
        router = LogRouter()
        gate = threading.Event()
        received = Collector()

        def blocking_sender(record):
            gate.wait(timeout=10)
            received(record)

        router.add_destination(
            "d", blocking_sender, queue_size=2, drop_policy=DropPolicy.DROP_OLDEST
        )
        router.add_rule(Rule("all", "msg", "regex", r".*", ("d",)))
        for i in range(6):
            router.route({"msg": f"m{i}"})
        gate.set()
        router.close(timeout=2)
        stats = router.stats().destinations["d"]
        self.assertGreater(stats["dropped"], 0)
        # Newest records survive under drop_oldest.
        msgs = [r["msg"] for r in received.records]
        self.assertIn("m5", msgs)
        self.assertNotIn("m1", msgs)


class TestAllDestinationsDown(unittest.TestCase):
    def test_failing_sinks_are_contained_and_counted(self):
        router = LogRouter()

        def boom(record):
            raise ConnectionError("destination unreachable")

        router.add_destination("dead1", boom)
        router.add_destination("dead2", boom)
        router.add_rule(Rule("all", "msg", "regex", r".*", ("dead1", "dead2")))
        for i in range(20):
            router.route({"msg": f"x{i}"})  # must not raise
        router.close()
        stats = router.stats()
        self.assertEqual(stats.routed, 20)
        for name in ("dead1", "dead2"):
            dest = stats.destinations[name]
            self.assertEqual(dest["failed"], 20)
            self.assertEqual(dest["delivered"], 0)


class TestBurst(unittest.TestCase):
    def test_burst_respects_buffer_bound(self):
        router = LogRouter()
        sink = Collector()
        queue_size = 50
        router.add_destination("out", sink, queue_size=queue_size)
        router.add_rule(Rule("all", "msg", "regex", r".*", ("out",)))

        burst = 10_000
        for i in range(burst):
            router.route({"msg": f"burst-{i}"})
        # Immediately after the burst, in-flight records are bounded.
        mid = router.stats().destinations["out"]
        self.assertLessEqual(mid["queued"], queue_size)
        self.assertEqual(mid["enqueued"] + mid["dropped"], burst)
        router.close()
        stats = router.stats().destinations["out"]
        self.assertEqual(stats["enqueued"] + stats["dropped"], burst)
        self.assertEqual(stats["delivered"], stats["enqueued"])
        self.assertEqual(len(sink), stats["delivered"])
        latency = stats["latency"]
        self.assertEqual(latency["count"], stats["delivered"])
        self.assertGreaterEqual(latency["max"], latency["avg"])


class TestStats(unittest.TestCase):
    def test_hit_distribution_and_latency_reported(self):
        router = LogRouter()
        sink = Collector()
        router.add_destination("out", sink)
        router.add_rule(Rule("errors", "level", "eq", "ERROR", ("out",)))
        router.add_rule(Rule("web", "source", "prefix", "nginx", ("out",)))
        router.route({"level": "ERROR", "source": "app"})
        router.route({"level": "INFO", "source": "nginx-1"})
        router.route({"level": "ERROR", "source": "nginx-2"})
        router.route({"level": "DEBUG", "source": "app"})
        router.close()
        stats = router.stats()
        self.assertEqual(stats.rule_hits, {"errors": 2, "web": 2})
        self.assertEqual(stats.routed, 3)
        self.assertEqual(stats.unmatched, 1)
        dest = stats.destinations["out"]
        self.assertEqual(dest["delivered"], 3)  # dedup: 3 records, not 4
        self.assertEqual(dest["latency"]["count"], 3)


if __name__ == "__main__":
    unittest.main()
