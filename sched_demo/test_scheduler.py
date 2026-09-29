"""Regression tests for the starvation-free scheduler.

Run:  python3 -m unittest sched_demo.test_scheduler -v
"""

from __future__ import annotations

import unittest

from . import naive, scheduler as sched
from .scheduler import DeadlockError, Scheduler, Work, Yield
from . import reproduce as repro


def run_seq(s: Scheduler):
    """CPU-run event stream (task names in dispatch order)."""
    return [ev[2] for ev in s.trace if ev[0] == "run"]


class BasicCases(unittest.TestCase):
    def test_single_coroutine(self):
        s = Scheduler(quantum=10)
        s.spawn("only", priority=0, gen=_burst_worker(25))
        s.run()
        # One preempted burst: run events of exactly 10, 10, 5 ticks.
        runs = [ev for ev in s.trace if ev[0] == "run"]
        self.assertEqual([e[4] - e[3] for e in runs], [10, 10, 5])
        self.assertEqual(s.now, 25)
        self.assertTrue(s.tasks["only"].finished)

    def test_all_same_priority_is_fifo(self):
        s = Scheduler(quantum=5)
        for i in range(4):
            s.spawn("t%d" % i, priority=1, gen=_burst_worker(12))
        s.run()
        # First round must be strict spawn order.
        first_round = [ev[2] for ev in s.trace if ev[0] == "run" and ev[1] == 1]
        self.assertEqual(first_round, ["t0", "t1", "t2", "t3"])

    def test_empty_ready_queue(self):
        # Nothing spawned: run() returns immediately, clock untouched.
        s = Scheduler(quantum=10)
        s.run()
        self.assertEqual(s.now, 0)
        self.assertEqual(s.trace, [])

    def test_priority_ordering(self):
        s = Scheduler(quantum=10)
        s.spawn("low", priority=2, gen=_burst_worker(5))
        s.spawn("high", priority=0, gen=_burst_worker(5))
        s.spawn("mid", priority=1, gen=_burst_worker(5))
        s.run()
        first_round = [ev[2] for ev in s.trace if ev[0] == "run" and ev[1] == 1]
        self.assertEqual(first_round, ["high", "mid", "low"])

    def test_yield_reenqueues_for_next_round(self):
        s = Scheduler(quantum=10)
        s.spawn("a", 1, gen=_yielder(2))
        s.spawn("b", 1, gen=_burst_worker(10))
        s.run()
        rounds = {}
        for ev in s.trace:
            if ev[0] == "run":
                rounds.setdefault(ev[1], []).append(ev[2])
        self.assertEqual(rounds[1], ["b"])        # a yielded at once
        self.assertEqual(rounds[2], ["a"])        # b finished in round 1
        self.assertEqual(rounds[3], ["a"])        # a's second burst


class Determinism(unittest.TestCase):
    def test_trace_depends_only_on_spawn_order_and_priorities(self):
        def build():
            sch = Scheduler(quantum=7)
            sch.spawn("L0", 2, _burst_worker(23))
            sch.spawn("H", 0, _burst_worker(15))
            sch.spawn("L1", 2, _burst_worker(11))
            sch.spawn("M", 1, _burst_worker(19))
            sch.run()
            return list(sch.trace), sch.now

        t1, n1 = build()
        t2, n2 = build()
        self.assertEqual(t1, t2)
        self.assertEqual(n1, n2)

    def test_swapping_spawn_order_changes_nothing_when_priorities_differ(self):
        def build():
            sch = Scheduler(quantum=7)
            sch.spawn("H", 0, _burst_worker(15))
            sch.spawn("L0", 2, _burst_worker(23))
            sch.spawn("M", 1, _burst_worker(19))
            sch.spawn("L1", 2, _burst_worker(11))
            sch.run()
            return sch.trace, sch.now

        a_trace, a_now = build()
        b = Scheduler(quantum=7)
        b.spawn("L0", 2, _burst_worker(23))
        b.spawn("H", 0, _burst_worker(15))
        b.spawn("L1", 2, _burst_worker(11))
        b.spawn("M", 1, _burst_worker(19))
        b.run()
        # Same priority multiset -> same ordered execution, regardless of
        # interleaving of spawn calls across priority classes.
        self.assertEqual(
            [e[2] for e in a_trace if e[0] == "run"],
            [e[2] for e in b.trace if e[0] == "run"],
        )
        self.assertEqual(a_now, b.now)


class Starvation(unittest.TestCase):
    def test_naive_scheduler_starves_lows(self):
        n_low, burst = 50, 1234
        nb = naive.NaiveScheduler()
        for i in range(n_low):
            nb.spawn("L%d" % i, priority=1, gen=_burst_worker(30))
        nb.spawn("H", priority=0, gen=_burst_worker(burst))
        nb.run()
        first_low = min(
            t.first_start for n, t in nb.tasks.items() if n != "H"
        )
        # The very first low-priority tick happens only after the full
        # high-priority burst: starvation reproduced.
        self.assertEqual(first_low, burst)

    def test_fixed_scheduler_serves_lows_in_first_round(self):
        n_low, quantum, burst = 50, 20, 123456
        s = Scheduler(quantum=quantum)
        for i in range(n_low):
            s.spawn("L%02d" % i, priority=1, gen=_burst_worker(30))
        s.spawn("H", priority=0, gen=_burst_worker(burst))
        s.run()
        # Every low is dispatched in round 1 before the hog gets a 2nd slice.
        first_round = [ev[2] for ev in s.trace if ev[0] == "run" and ev[1] == 1]
        self.assertEqual(first_round[0], "H")
        lows_round1 = set(n for n in first_round if n != "H")
        self.assertEqual(len(lows_round1), n_low)
        # Bound is tight: the last low waits for exactly (n-1)*q service.
        max_wait = max(
            d - r
            for name, t in s.tasks.items()
            if name != "H"
            for r0, r, d in t.waits
            if r == 0
        )
        self.assertEqual(max_wait, n_low * quantum)

    def test_wait_bound_holds_for_many_configurations(self):
        for q in (1, 5, 17):
            for n in (2, 8, 33):
                s = Scheduler(quantum=q)
                for i in range(n):
                    s.spawn("T%02d" % i, i % 3, _burst_worker(q * 3 + i))
                s.run()
                per_round = {}
                for ev in s.trace:
                    if ev[0] == "run":
                        per_round.setdefault(ev[1], []).append(
                            ev[4] - ev[3]
                        )
                for rno, used in per_round.items():
                    # No round consumes more than n*q CPU.
                    self.assertLessEqual(sum(used), n * q)


class PriorityInversion(unittest.TestCase):
    def test_naive_inversion_scales_with_medium_load(self):
        nb = naive.NaiveScheduler()
        repro.build_inversion(nb, n_medium=4, medium_burst=500, cs_len=8)
        nb.run()
        block, wake = repro.block_window(nb.trace, "H")
        self.assertGreaterEqual(wake - block, 4 * 500)

    def test_fixed_inversion_is_bounded(self):
        for medium_burst in (100, 1000, 10000):
            s = Scheduler(quantum=10, inherit=True)
            repro.build_inversion(s, n_medium=4, medium_burst=medium_burst, cs_len=8)
            s.run()
            block, wake = repro.block_window(s.trace, "H")
            self.assertLessEqual(wake - block, 5 * 10 + 8)

    def test_inheritance_changes_ordering_vs_no_inheritance(self):
        # L holds the lock for > 1 quantum while H waits; in round 3 the
        # holder must be promoted ahead of medium hogs only with inheritance.
        def build(inherit):
            sch = Scheduler(quantum=10, inherit=inherit)
            repro.build_inversion(sch, n_medium=3, medium_burst=100, cs_len=25)
            sch.run()
            return [ev[2] for ev in sch.trace if ev[0] == "run" and ev[1] == 3]

        with_inh = build(True)
        without_inh = build(False)
        self.assertIn("L", with_inh)
        # With inheritance L is promoted: it runs before every M in round 3.
        self.assertLess(with_inh.index("L"), with_inh.index("M0"))
        # Without inheritance the medium hogs come first in that round.
        self.assertLess(without_inh.index("M0"), without_inh.index("L"))


class DeadlockDetection(unittest.TestCase):
    def test_blocked_forever_raises(self):
        s = Scheduler(quantum=10)
        lk = s.new_lock("x")
        holder = s.spawn("h", 0, _holds_forever(lk))
        lk.holder = holder
        s.spawn("w", 1, _blocks_on(lk))
        with self.assertRaises(DeadlockError):
            s.run()

    def test_cyclic_deadlock_raises(self):
        s = Scheduler(quantum=10)
        a_lock = s.new_lock("a")
        b_lock = s.new_lock("b")
        s.spawn("A", 0, _cycle(a_lock, b_lock))
        s.spawn("B", 1, _cycle(b_lock, a_lock))
        with self.assertRaises(DeadlockError):
            s.run()


class LockWaiters(unittest.TestCase):
    def test_wake_order_is_fifo(self):
        s = Scheduler(quantum=10)
        lk = s.new_lock("res")

        def holder():
            yield lk.acquire()
            yield Work(5)
            yield Yield()
            yield Work(5)
            yield lk.release()

        def waiter():
            yield Yield()       # ensure holder owns the lock first
            yield lk.acquire()
            yield Work(1)
            yield lk.release()

        s.spawn("L", 2, holder())
        for i in range(3):
            s.spawn("W%d" % i, i, waiter())
        s.run()
        wakes = [ev[1] for ev in s.trace if ev[0] == "wake"]
        self.assertEqual(wakes, ["W0", "W1", "W2"])


class ReleaseValidation(unittest.TestCase):
    def test_releasing_unheld_lock_raises(self):
        s = Scheduler(quantum=10)
        lk = s.new_lock("x")
        s.spawn("t", 0, _bad_release(lk))
        with self.assertRaises(RuntimeError):
            s.run()


# --------------------------------------------------------------------------- #
# Task bodies
# --------------------------------------------------------------------------- #


def _burst_worker(total):
    def body():
        yield Work(total)

    return body()


def _yielder(rounds):
    def body():
        for _ in range(rounds):
            yield Yield()
            yield Work(3)

    return body()


def _holds_forever(lk):
    def body():
        yield lk.acquire()
        yield Work(1)
        yield Yield()  # never releases

    return body()


def _blocks_on(lk):
    def body():
        yield lk.acquire()

    return body()


def _cycle(first, second):
    def body():
        yield first.acquire()
        yield Yield()
        yield second.acquire()

    return body()


def _bad_release(lk):
    def body():
        yield lk.release()

    return body()


if __name__ == "__main__":
    unittest.main()
