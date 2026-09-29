"""Edge-case and invariant tests for fair_sync (stdlib unittest)."""

import threading
import time
import unittest

from fair_sync import BrokenBarrierError, FairBarrier, FairSemaphore


def run_threads(threads):
    for t in threads:
        t.start()
    for t in threads:
        t.join()


class SemaphoreBasicTests(unittest.TestCase):
    def test_initial_value_validation(self):
        for bad in (0, -1, 1.5, "2"):
            with self.assertRaises(ValueError):
                FairSemaphore(bad)

    def test_basic_acquire_release(self):
        sem = FairSemaphore(2)
        self.assertTrue(sem.acquire())
        self.assertTrue(sem.acquire())
        self.assertEqual(sem.value, 0)
        sem.release()
        self.assertEqual(sem.value, 1)
        sem.release()
        self.assertEqual(sem.value, 2)

    def test_release_count_validation(self):
        sem = FairSemaphore(1)
        with self.assertRaises(ValueError):
            sem.release()  # nothing held
        sem.acquire()
        sem.release()
        with self.assertRaises(ValueError):
            sem.release()  # over-release
        self.assertEqual(sem.value, 1)  # state untouched by failed release

    def test_acquire_timeout_returns_false(self):
        sem = FairSemaphore(1)
        sem.acquire()
        start = time.monotonic()
        self.assertFalse(sem.acquire(timeout=0.2))
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.18)
        self.assertLess(elapsed, 1.0)

    def test_timeout_zero_is_nonblocking(self):
        sem = FairSemaphore(1)
        self.assertTrue(sem.acquire(timeout=0))
        self.assertFalse(sem.acquire(timeout=0))

    def test_timeout_then_release_no_permit_leak(self):
        # A timed-out waiter must not consume a permit released later.
        sem = FairSemaphore(1)
        sem.acquire()
        self.assertFalse(sem.acquire(timeout=0.05))
        self.assertEqual(sem.waiters, 0)
        sem.release()
        self.assertEqual(sem.value, 1)
        self.assertTrue(sem.acquire(timeout=0))  # permit is really free
        sem.release()
        with self.assertRaises(ValueError):
            sem.release()  # count still consistent

    def test_concurrent_timeout_and_grant_prefers_grant(self):
        # If the permit arrives exactly at the deadline, the grant wins
        # and no permit is lost.
        sem = FairSemaphore(1)
        sem.acquire()
        acquired = []

        def waiter():
            acquired.append(sem.acquire(timeout=2.0))

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.1)  # ensure queued
        sem.release()
        t.join()
        self.assertEqual(acquired, [True])
        self.assertEqual(sem.value, 0)
        sem.release()
        self.assertEqual(sem.value, 1)

    def test_context_manager_releases_on_exception(self):
        sem = FairSemaphore(1)
        with self.assertRaises(RuntimeError):
            with sem:
                raise RuntimeError("boom")
        self.assertEqual(sem.value, 1)  # permit returned despite exception
        self.assertTrue(sem.acquire(timeout=0))

    def test_fifo_wakeup_order(self):
        # Threads queued in a known order must be granted in that order.
        sem = FairSemaphore(1)
        sem.acquire()  # hold the only permit
        order = []
        order_lock = threading.Lock()

        def worker(tag):
            sem.acquire()
            with order_lock:
                order.append(tag)
            time.sleep(0.01)
            sem.release()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
            time.sleep(0.05)  # deterministic arrival order 0..4
        deadline = time.monotonic() + 2.0
        while sem.waiters < 5 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(sem.waiters, 5)
        sem.release()
        for t in threads:
            t.join()
        self.assertEqual(order, [0, 1, 2, 3, 4])

    def test_holders_never_exceed_max_under_contention(self):
        max_permits = 3
        sem = FairSemaphore(max_permits)
        current = 0
        high_water = 0
        violations = []
        lock = threading.Lock()
        stop = time.monotonic() + 1.5

        def worker():
            nonlocal current, high_water
            while time.monotonic() < stop:
                self.assertTrue(sem.acquire(timeout=2.0))
                with lock:
                    current += 1
                    if current > max_permits:
                        violations.append(current)
                    high_water = max(high_water, current)
                time.sleep(0.0005)
                with lock:
                    current -= 1
                sem.release()

        threads = [threading.Thread(target=worker) for _ in range(16)]
        run_threads(threads)
        self.assertEqual(violations, [])
        self.assertEqual(high_water, max_permits)  # full utilization reached
        self.assertEqual(sem.value, max_permits)


class BarrierBasicTests(unittest.TestCase):
    def test_parties_zero_or_negative_raises(self):
        for bad in (0, -3, 2.5, "4"):
            with self.assertRaises(ValueError):
                FairBarrier(bad)

    def test_single_party_passes_immediately(self):
        barrier = FairBarrier(1)
        start = time.monotonic()
        self.assertEqual(barrier.wait(timeout=1.0), 0)
        self.assertLess(time.monotonic() - start, 0.1)

    def test_all_arrive_before_any_release(self):
        parties = 8
        barrier = FairBarrier(parties)
        arrived = 0
        lock = threading.Lock()
        violations = []

        def worker():
            nonlocal arrived
            with lock:
                arrived += 1
            barrier.wait()
            with lock:
                if arrived != parties:
                    violations.append(arrived)

        run_threads([threading.Thread(target=worker) for _ in range(parties)])
        self.assertEqual(violations, [])

    def test_arrival_index_reflects_order(self):
        parties = 5
        barrier = FairBarrier(parties)
        indices = [None] * parties

        def worker(i):
            indices[i] = barrier.wait()

        threads = []
        for i in range(parties):
            t = threading.Thread(target=worker, args=(i,))
            t.start()
            threads.append(t)
            time.sleep(0.03)  # deterministic arrival order
        for t in threads:
            t.join()
        self.assertEqual(indices, list(range(parties)))

    def test_timeout_breaks_generation_then_recovers(self):
        barrier = FairBarrier(3)
        errors = []

        def waiter():
            try:
                barrier.wait(timeout=0.15)
            except BrokenBarrierError:
                errors.append(1)

        # Only 2 of 3 parties arrive -> generation breaks.
        run_threads([threading.Thread(target=waiter) for _ in range(2)])
        self.assertEqual(len(errors), 2)

        # Barrier auto-resets: a fresh full generation succeeds.
        results = []
        run_threads([
            threading.Thread(target=lambda: results.append(barrier.wait(timeout=2.0)))
            for _ in range(3)
        ])
        self.assertEqual(sorted(results), [0, 1, 2])

    def test_no_duplicate_release_per_generation(self):
        parties = 6
        rounds = 50
        barrier = FairBarrier(parties)
        released = [0] * rounds
        lock = threading.Lock()
        errors = []

        def worker():
            for r in range(rounds):
                try:
                    barrier.wait(timeout=5.0)
                except BrokenBarrierError:
                    errors.append("broken")
                    return
                with lock:
                    released[r] += 1
                    if released[r] > parties:
                        errors.append(f"round {r} released {released[r]}")

        run_threads([threading.Thread(target=worker) for _ in range(parties)])
        self.assertEqual(errors, [])
        self.assertEqual(released, [parties] * rounds)
        self.assertEqual(barrier.generation, rounds)

    def test_exception_path_exit_does_not_corrupt_barrier(self):
        # A thread raising *after* wait() must not affect later generations.
        barrier = FairBarrier(2)
        errors = []

        def bad():
            barrier.wait(timeout=2.0)
            raise RuntimeError("leave generation 0 abnormally")

        def good():
            barrier.wait(timeout=2.0)

        t1 = threading.Thread(target=bad)
        t2 = threading.Thread(target=good)
        t1.start(); t2.start(); t1.join(); t2.join()

        def safe_wait():
            try:
                barrier.wait(timeout=2.0)
            except BrokenBarrierError:
                errors.append(1)

        run_threads([threading.Thread(target=safe_wait) for _ in range(2)])
        self.assertEqual(errors, [])
        self.assertEqual(barrier.generation, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
