"""Reproduction + regression + stress tests for the barrier fix.

Run:  python3 -m unittest discover -s tests -v

TestBuggyBarrierRepro *documents* the production bug: those tests pass when
the buggy implementation fails (lost wakeup / state residue), proving the
reproduction is stable.
TestFixedBarrier contains the regression assertions for the fixed version.
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from barrier import Barrier, BrokenBarrierError
from barrier_buggy import BuggyBarrier


def run_rounds(barrier, parties, rounds, violations, start_event=None):
    """Worker harness: each thread records cumulative arrivals before wait()
    and checks after wait() that nobody was released early."""
    arrived = 0
    arrived_lock = threading.Lock()

    def worker():
        nonlocal arrived
        for r in range(rounds):
            with arrived_lock:
                arrived += 1
            barrier.wait()
            with arrived_lock:
                # release for round r is only legal if all parties of this
                # round have arrived
                if arrived < parties * (r + 1):
                    violations.append(
                        f"round {r}: released with only {arrived} arrivals "
                        f"(need {parties * (r + 1)})"
                    )

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(parties)]
    for t in threads:
        t.start()
    if start_event is not None:
        start_event.set()
    return threads


class TestBuggyBarrierRepro(unittest.TestCase):
    """These tests PASS when the buggy barrier exhibits the bug."""

    def test_repro_lost_wakeup_under_reuse(self):
        """High-frequency reuse: buggy barrier deadlocks (lost wakeup)."""
        hang_detected = False
        violations = []
        for _attempt in range(3):
            barrier = BuggyBarrier(parties=6)
            threads = run_rounds(barrier, 6, 500, violations)
            deadline = time.monotonic() + 5.0
            for t in threads:
                t.join(max(0.0, deadline - time.monotonic()))
            if any(t.is_alive() for t in threads):
                hang_detected = True
                break
            if violations:
                break
        self.assertTrue(
            hang_detected or violations,
            "bug not reproduced: expected deadlock or early release",
        )
        print(f"\n[repro] lost-wakeup hang={hang_detected}, "
              f"early-release violations={len(violations)}")

    def test_repro_state_residue_after_killed_thread(self):
        """A participant arrives then dies: count leaks into the next round
        and the buggy barrier releases with one participant missing."""
        barrier = BuggyBarrier(parties=3)
        # simulate a thread that arrived (count += 1) and was then killed
        with barrier.cond:
            barrier.count += 1
        released = []
        start = threading.Event()

        def worker(i):
            start.wait()
            barrier.wait()
            released.append(i)

        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(2)]  # only 2 of 3 real participants
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join(3.0)
        # bug: barrier tripped with only 2 of 3 participants present
        self.assertEqual(
            2, len(released),
            "bug not reproduced: expected early release from leaked count",
        )
        print("\n[repro] state residue: barrier released with "
              f"{len(released)}/3 participants after a thread died")


class TestFixedBarrier(unittest.TestCase):
    def assert_clean_state(self, barrier, expected_generation):
        """State must be fully reset after a release."""
        self.assertEqual(0, barrier.n_waiting, "count not reset")
        self.assertFalse(barrier.broken, "barrier unexpectedly broken")
        self.assertEqual(expected_generation, barrier.generation,
                         "generation not advanced")

    def test_single_party(self):
        barrier = Barrier(1)
        for i in range(1000):
            barrier.wait()  # must return immediately
            self.assert_clean_state(barrier, i + 1)

    def test_all_parties_must_arrive(self):
        parties, rounds = 8, 300
        barrier = Barrier(parties)
        violations = []
        threads = run_rounds(barrier, parties, rounds, violations)
        for t in threads:
            t.join(30.0)
        self.assertFalse(any(t.is_alive() for t in threads), "deadlock")
        self.assertEqual([], violations)
        self.assert_clean_state(barrier, rounds)

    def test_concurrent_arrival(self):
        """All threads arrive at (nearly) the same instant, many rounds."""
        parties, rounds = 16, 500
        barrier = Barrier(parties)
        violations = []
        gate = threading.Event()

        def worker():
            gate.wait()
            for r in range(rounds):
                barrier.wait()

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(parties)]
        for t in threads:
            t.start()
        gate.set()  # release all arrivals simultaneously
        for t in threads:
            t.join(30.0)
        self.assertFalse(any(t.is_alive() for t in threads), "deadlock")
        self.assert_clean_state(barrier, rounds)

    def test_reuse_thousands_of_rounds_stress(self):
        parties, rounds = 8, 2000
        barrier = Barrier(parties)
        violations = []
        t0 = time.perf_counter()
        threads = run_rounds(barrier, parties, rounds, violations)
        for t in threads:
            t.join(60.0)
        elapsed = time.perf_counter() - t0
        self.assertFalse(any(t.is_alive() for t in threads), "deadlock")
        self.assertEqual([], violations)
        self.assert_clean_state(barrier, rounds)
        print(f"\n[stress] parties={parties} rounds={rounds} "
              f"elapsed={elapsed:.3f}s "
              f"({rounds / elapsed:.0f} rounds/s, "
              f"{elapsed / rounds * 1e6:.1f} us/round)")

    def test_participant_dies_waiters_released(self):
        """One participant exits with an exception mid-stream; the rest must
        not block forever (timeout path breaks the barrier for everyone)."""
        parties = 4
        barrier = Barrier(parties)
        errors = []
        done = threading.Event()

        def survivor():
            try:
                while True:
                    barrier.wait(timeout=1.0)
            except BrokenBarrierError as e:
                errors.append(e)

        def doomed():
            try:
                for _ in range(5):
                    barrier.wait()
                raise RuntimeError("participant killed")  # never arrives again
            except RuntimeError:
                pass  # thread dies here; survivors must still be released

        threads = [threading.Thread(target=survivor, daemon=True)
                   for _ in range(parties - 1)]
        threads.append(threading.Thread(target=doomed, daemon=True))
        for t in threads:
            t.start()
        deadline = time.monotonic() + 10.0
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        self.assertFalse(any(t.is_alive() for t in threads),
                         "survivors blocked forever after participant died")
        self.assertEqual(parties - 1, len(errors))
        self.assertTrue(barrier.broken)
        done.set()

    def test_killed_thread_plus_abort(self):
        """Participant never arrives (killed); abort() must release waiters."""
        barrier = Barrier(4)
        errors = []

        def waiter():
            try:
                barrier.wait()  # no timeout: relies on abort()
            except BrokenBarrierError as e:
                errors.append(e)

        threads = [threading.Thread(target=waiter, daemon=True)
                   for _ in range(3)]
        for t in threads:
            t.start()
        time.sleep(0.3)
        self.assertEqual(3, barrier.n_waiting)
        barrier.abort()
        for t in threads:
            t.join(3.0)
        self.assertFalse(any(t.is_alive() for t in threads),
                         "abort() failed to release waiters")
        self.assertEqual(3, len(errors))
        self.assertTrue(barrier.broken)

    def test_timeout_breaks_barrier_for_everyone(self):
        barrier = Barrier(3)
        errors = []

        def waiter(with_timeout):
            try:
                barrier.wait(timeout=0.3 if with_timeout else None)
            except BrokenBarrierError as e:
                errors.append(e)

        threads = [threading.Thread(target=waiter, args=(i == 0,), daemon=True)
                   for i in range(2)]  # 3rd participant never shows up
        for t in threads:
            t.start()
        for t in threads:
            t.join(5.0)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(2, len(errors), "timeout must release all waiters")
        self.assertTrue(barrier.broken)

    def test_reset_after_abort_allows_reuse(self):
        parties, rounds = 4, 100
        barrier = Barrier(parties)
        barrier.abort()
        self.assertTrue(barrier.broken)
        barrier.reset()
        self.assertFalse(barrier.broken)
        self.assertEqual(0, barrier.n_waiting)
        violations = []
        threads = run_rounds(barrier, parties, rounds, violations)
        for t in threads:
            t.join(30.0)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual([], violations)
        # generation: 1 (reset) + rounds releases
        self.assert_clean_state(barrier, 1 + rounds)


if __name__ == "__main__":
    unittest.main()
