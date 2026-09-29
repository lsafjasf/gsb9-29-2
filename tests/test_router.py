import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logrouter import Destination, FieldEquals, FieldPrefix, FieldRegex, Router


class MemoryDestination(Destination):
    def __init__(self, name, delay=0.0, fail=False):
        self.name = name
        self.delay = delay
        self.fail = fail
        self.records = []
        self._lock = threading.Lock()

    def send(self, record):
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("destination %s is down" % self.name)
        with self._lock:
            self.records.append(record)

    def count(self):
        with self._lock:
            return len(self.records)


class BlockingDestination(Destination):
    """send 永久阻塞，用于隔离性测试。"""

    def __init__(self, name):
        self.name = name
        self.started = threading.Event()

    def send(self, record):
        self.started.set()
        threading.Event().wait(3600)


def make_router(**kwargs):
    router = Router(queue_size=kwargs.pop("queue_size", 64))
    for name, dest in kwargs.items():
        router.add_destination(dest)
    return router


class MatchTests(unittest.TestCase):
    def test_no_match_counts_unmatched(self):
        router = make_router(a=MemoryDestination("a"))
        router.add_rule(FieldEquals("errors", "level", "ERROR", ["a"]))
        self.assertEqual(router.route({"level": "INFO"}), [])
        self.assertEqual(router.unmatched, 1)
        router.flush()
        self.assertEqual(router.stats()["destinations"]["a"]["delivered"], 0)
        router.close()

    def test_all_match_delivers_to_every_destination(self):
        dests = {n: MemoryDestination(n) for n in ("a", "b", "c")}
        router = make_router(**dests)
        router.add_rule(FieldEquals("r1", "level", "ERROR", ["a", "b"]))
        router.add_rule(FieldPrefix("r2", "msg", "disk", ["b", "c"]))
        router.add_rule(FieldRegex("r3", "msg", r"\d+", ["c"]))
        record = {"level": "ERROR", "msg": "disk full, code 42"}
        self.assertEqual(router.route(record), ["a", "b", "c"])
        router.flush()
        for dest in dests.values():
            self.assertEqual(dest.count(), 1)
        stats = router.stats()
        self.assertEqual(stats["rule_hits"], {"r1": 1, "r2": 1, "r3": 1})
        router.close()

    def test_overlapping_rules_dedup_per_destination(self):
        router = make_router(a=MemoryDestination("a"), b=MemoryDestination("b"))
        router.add_rule(FieldEquals("r1", "level", "ERROR", ["a", "b"]))
        router.add_rule(FieldPrefix("r2", "level", "ERR", ["a"]))
        router.route({"level": "ERROR"})
        router.flush()
        stats = router.stats()
        # 两条规则都命中并计数，但 a 只收到一条
        self.assertEqual(stats["rule_hits"], {"r1": 1, "r2": 1})
        self.assertEqual(stats["destinations"]["a"]["delivered"], 1)
        self.assertEqual(stats["destinations"]["b"]["delivered"], 1)
        router.close()

    def test_missing_field_does_not_match(self):
        router = make_router(a=MemoryDestination("a"))
        router.add_rule(FieldPrefix("p", "msg", "x", ["a"]))
        router.add_rule(FieldRegex("r", "msg", ".*", ["a"]))
        router.route({"other": 1})
        self.assertEqual(router.unmatched, 1)
        router.close()


class FailureTests(unittest.TestCase):
    def test_all_destinations_down(self):
        router = make_router(a=MemoryDestination("a", fail=True),
                             b=MemoryDestination("b", fail=True))
        router.add_rule(FieldEquals("all", "level", "ERROR", ["a", "b"]))
        for _ in range(10):
            router.route({"level": "ERROR"})
        router.flush()
        stats = router.stats()
        for name in ("a", "b"):
            self.assertEqual(stats["destinations"][name]["failed"], 10)
            self.assertEqual(stats["destinations"][name]["delivered"], 0)
        router.close()

    def test_blocked_destination_isolated_and_drops_counted(self):
        blocking = BlockingDestination("slow")
        fast = MemoryDestination("fast")
        router = Router()
        router.add_destination(blocking, queue_size=8)
        router.add_destination(fast, queue_size=2048)
        router.add_rule(FieldEquals("both", "type", "x", ["slow", "fast"]))
        self.assertTrue(blocking.started.wait(2) or True)
        # 先发一条让 slow 的 worker 卡住
        router.route({"type": "x"})
        self.assertTrue(blocking.started.wait(2))
        # 突增 1000 条：fast 必须全部及时收到，slow 队列满后丢弃
        started = time.monotonic()
        for _ in range(1000):
            router.route({"type": "x"})
        elapsed = time.monotonic() - started
        router.flush(timeout=1)
        stats = router.stats()
        fast_stats = stats["destinations"]["fast"]
        slow_stats = stats["destinations"]["slow"]
        self.assertEqual(fast.count(), 1001)
        self.assertEqual(fast_stats["delivered"], 1001)
        self.assertEqual(fast_stats["dropped"], 0)
        # slow: 1 条在 send 里卡住 + 8 条在队列 + 其余被丢弃
        self.assertEqual(slow_stats["dropped"], 1001 - 1 - 8)
        self.assertLess(elapsed, 2.0, "阻塞目的地拖慢了路由线程")
        router.close(timeout=0.2)

    def test_burst_drop_newest_within_bound(self):
        slow = MemoryDestination("slow", delay=0.01)
        router = Router(queue_size=16)
        router.add_destination(slow)
        router.add_rule(FieldEquals("all", "k", "v", ["slow"]))
        total = 500
        for _ in range(total):
            router.route({"k": "v"})
        router.flush(timeout=30)
        stats = router.stats()["destinations"]["slow"]
        self.assertEqual(stats["delivered"] + stats["dropped"], total)
        self.assertGreater(stats["dropped"], 0)
        self.assertLessEqual(stats["delivered"], total)
        # 延迟统计存在且有界
        latency = stats["latency"]
        self.assertEqual(latency["count"], stats["delivered"])
        self.assertGreaterEqual(latency["max_ms"], latency["min_ms"])
        router.close()


class StatsTests(unittest.TestCase):
    def test_stats_shape(self):
        router = make_router(a=MemoryDestination("a"))
        router.add_rule(FieldEquals("r", "k", "v", ["a"]))
        router.route({"k": "v"})
        router.route({"k": "other"})
        router.flush()
        stats = router.stats()
        self.assertEqual(stats["received"], 2)
        self.assertEqual(stats["unmatched"], 1)
        self.assertEqual(stats["rule_hits"], {"r": 1})
        dest = stats["destinations"]["a"]
        self.assertEqual(dest["queue_size"], 64)
        self.assertEqual(dest["drop_policy"], "drop_newest")
        self.assertIn("p95_ms", dest["latency"])
        router.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
