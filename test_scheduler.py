import time
import unittest

from scheduler import Scheduler, sleep, wait


class TestEdgeCases(unittest.TestCase):
    def test_no_coroutines(self):
        sched = Scheduler()
        stats = sched.run()
        self.assertEqual(stats["tasks_total"], 0)
        self.assertEqual(stats["rounds"], 0)
        self.assertEqual(stats["context_switches"], 0)
        self.assertEqual(stats["deadlocked"], [])

    def test_single_coroutine(self):
        def worker():
            yield
            yield
            return 42

        sched = Scheduler()
        task = sched.create_task(worker(), name="solo")
        stats = sched.run()
        self.assertTrue(task.finished)
        self.assertEqual(task.result, 42)
        self.assertEqual(stats["tasks_finished"], 1)
        self.assertEqual(stats["context_switches"], 3)

    def test_massive_coroutines(self):
        n = 20000

        def worker():
            yield
            yield

        sched = Scheduler()
        for i in range(n):
            sched.create_task(worker(), name=f"w{i}")
        stats = sched.run()
        self.assertEqual(stats["tasks_finished"], n)
        self.assertEqual(stats["rounds"], 3)
        self.assertEqual(stats["round_task_counts"][0], n)
        self.assertEqual(stats["context_switches"], 3 * n)

    def test_wake_finished_coroutine(self):
        sched = Scheduler()
        ev = sched.event()

        def short():
            yield

        task = sched.create_task(short(), name="short")
        sched.run()
        self.assertTrue(task.finished)
        self.assertFalse(sched.wake_task(task))  # no-op, must not crash
        ev.set()  # set with zero waiters: no-op, must not crash
        stats = sched.run()
        self.assertEqual(stats["tasks_finished"], 1)

    def test_wake_sleeping_task_early(self):
        sched = Scheduler()
        done = []

        def sleeper():
            yield sleep(60.0)  # would oversleep the whole test without wake
            done.append("sleeper")

        task = sched.create_task(sleeper(), name="sleeper")
        # step once so the task goes to sleep
        sched._step(sched._ready.popleft())
        self.assertEqual(task.state, "sleeping")
        self.assertTrue(sched.wake_task(task))
        self.assertEqual(task.state, "ready")
        stats = sched.run()
        self.assertEqual(done, ["sleeper"])
        self.assertEqual(stats["deadlocked"], [])


class TestFairness(unittest.TestCase):
    def test_round_robin_interleaving(self):
        order = []

        def make(tag):
            def worker():
                for _ in range(3):
                    order.append(tag)
                    yield
            return worker()

        sched = Scheduler()
        sched.create_task(make("A"), name="A")
        sched.create_task(make("B"), name="B")
        sched.create_task(make("C"), name="C")
        sched.run()
        self.assertEqual(order, ["A", "B", "C"] * 3)

    def test_starvation_detection(self):
        def hog():
            start = time.perf_counter()
            while time.perf_counter() - start < 0.10:
                pass  # never yields: must be caught by the run-time threshold
            yield

        def nice():
            yield

        sched = Scheduler(max_run_time=0.02)
        sched.create_task(hog(), name="hog")
        sched.create_task(nice(), name="nice")
        stats = sched.run()
        self.assertEqual(stats["tasks_finished"], 2)
        self.assertTrue(stats["violations"], "long non-yielding run not reported")
        violation = stats["violations"][0]
        self.assertEqual(violation["task"], "hog")
        self.assertGreater(violation["run_time"], 0.02)
        self.assertFalse(any(v["task"] == "nice" for v in stats["violations"]))

    def test_sleep_wake_ordering(self):
        woken = []

        def make(tag, delay):
            def worker():
                yield sleep(delay)
                woken.append(tag)
            return worker()

        sched = Scheduler()
        sched.create_task(make("slow", 0.05), name="slow")
        sched.create_task(make("fast", 0.01), name="fast")
        sched.run()
        self.assertEqual(woken, ["fast", "slow"])


class TestWakeupTiming(unittest.TestCase):
    def test_wake_before_sleep_not_lost(self):
        order = []
        sched = Scheduler()
        ev = sched.event()

        def setter():
            ev.set()  # set BEFORE anyone waits
            order.append("set")
            yield

        def waiter():
            order.append("before-wait")
            yield wait(ev)  # flag already set: must not block
            order.append("after-wait")

        sched.create_task(setter(), name="setter")
        sched.create_task(waiter(), name="waiter")
        stats = sched.run()
        self.assertEqual(order, ["set", "before-wait", "after-wait"])
        self.assertEqual(stats["tasks_finished"], 2)
        self.assertEqual(stats["deadlocked"], [])

    def test_wake_during_sleep(self):
        order = []
        sched = Scheduler()
        ev = sched.event()

        def waiter():
            order.append("waiting")
            yield wait(ev)
            order.append("woken")

        def waker():
            yield  # let the waiter register first
            order.append("setting")
            ev.set()

        sched.create_task(waiter(), name="waiter")
        sched.create_task(waker(), name="waker")
        stats = sched.run()
        self.assertEqual(order, ["waiting", "setting", "woken"])
        self.assertEqual(stats["tasks_finished"], 2)
        self.assertEqual(stats["deadlocked"], [])

    def test_multiple_waiters_all_woken(self):
        sched = Scheduler()
        ev = sched.event()
        woken = []

        def make_waiter(tag):
            def worker():
                yield wait(ev)
                woken.append(tag)
            return worker()

        def waker():
            yield
            ev.set()

        for i in range(5):
            sched.create_task(make_waiter(i), name=f"waiter{i}")
        sched.create_task(waker(), name="waker")
        stats = sched.run()
        self.assertEqual(sorted(woken), list(range(5)))
        self.assertEqual(stats["tasks_finished"], 6)

    def test_deadlock_reported_not_hung(self):
        sched = Scheduler()
        ev = sched.event()

        def doomed():
            yield wait(ev)  # nobody ever sets it

        sched.create_task(doomed(), name="doomed")
        stats = sched.run()  # must return instead of hanging forever
        self.assertEqual(stats["deadlocked"], ["doomed"])
        self.assertEqual(stats["tasks_finished"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
