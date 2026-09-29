"""Regression suite for the fixed reusable barrier (barrier.py).

Covers:
  * parties = 1
  * all-parties-required (no early release)
  * full state reset after every release (asserted per round)
  * high-frequency reuse (fresh threads every round, and 1000+ rounds
    with long-lived threads)
  * concurrent arrival stress with round-skew detection
  * exception path: timeout aborts the barrier and releases all waiters
  * killed thread: abort() releases the remaining waiters (no deadlock)
  * reset() restores full usability after a break

Run:  python3 test_barrier.py -v
"""

import threading
import time
import unittest

from barrier import Barrier, BarrierTimeoutError, BrokenBarrierError


def run_threads(n, target, join_timeout=15.0):
    threads = [threading.Thread(target=target, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(join_timeout)
    return threads


class TestBasicContract(unittest.TestCase):
    def test_parties_one(self):
        bar = Barrier(1)
        for expected_gen in range(1, 2001):
            self.assertEqual(bar.wait(), 0)
            # state fully reset after every release
            self.assertEqual(bar.n_waiting, 0)
            self.assertFalse(bar.broken)
            self.assertEqual(bar.generation, expected_gen)

    def test_invalid_parties(self):
        with self.assertRaises(ValueError):
            Barrier(0)

    def test_no_release_until_all_arrive(self):
        parties = 6
        bar = Barrier(parties)
        released = []
        lock = threading.Lock()

        def worker(idx):
            bar.wait()
            with lock:
                released.append(idx)

        threads = run_threads(parties - 1, worker, join_timeout=0.5)
        # N-1 threads are parked: nobody may be released yet.
        self.assertEqual(released, [])
        self.assertTrue(all(t.is_alive() for t in threads))
        self.assertEqual(bar.n_waiting, parties - 1)

        bar.wait()  # last arrival opens the gate
        for t in threads:
            t.join(5.0)
        self.assertEqual(sorted(released), list(range(parties - 1)))
        self.assertEqual(bar.n_waiting, 0)
        self.assertFalse(bar.broken)

    def test_unique_leader_index_per_round(self):
        parties = 8
        bar = Barrier(parties)
        leaders = []
        lock = threading.Lock()

        def worker(idx):
            if bar.wait() == 0:
                with lock:
                    leaders.append(idx)

        run_threads(parties, worker)
        self.assertEqual(len(leaders), 1)


class TestStateResetAndReuse(unittest.TestCase):
    def test_state_reset_every_round_fresh_threads(self):
        """High-frequency reuse: brand-new thread set each round, assert
        full reset after every single round."""
        parties = 4
        rounds = 300
        bar = Barrier(parties)
        for r in range(rounds):
            released = [0]
            lock = threading.Lock()

            def worker(idx):
                bar.wait()
                with lock:
                    released[0] += 1

            run_threads(parties, worker)
            self.assertEqual(released[0], parties, "round %d" % r)
            self.assertEqual(bar.n_waiting, 0, "round %d" % r)
            self.assertFalse(bar.broken, "round %d" % r)
            self.assertEqual(bar.generation, r + 1, "round %d" % r)

    def test_thousand_rounds_long_lived_threads(self):
        """Continuous reuse: same threads, 2000 rounds, no carryover."""
        parties = 8
        rounds = 2000
        bar = Barrier(parties)
        progress = [0] * parties
        errors = []

        def worker(idx):
            try:
                for _ in range(rounds):
                    bar.wait()
                    progress[idx] += 1
                    # no thread may lap another: detects state carried
                    # into the next round
                    if progress[idx] - min(progress) > 1:
                        errors.append("thread %d lapped" % idx)
                        return
            except Exception as exc:  # pragma: no cover - failure path
                errors.append("thread %d: %r" % (idx, exc))

        start = time.monotonic()
        run_threads(parties, worker, join_timeout=60.0)
        elapsed = time.monotonic() - start

        self.assertEqual(errors, [])
        self.assertEqual(progress, [rounds] * parties)
        self.assertEqual(bar.n_waiting, 0)
        self.assertFalse(bar.broken)
        self.assertEqual(bar.generation, rounds)
        print("\n[stress] %d threads x %d rounds = %d waits in %.2fs "
              "(%.0f rounds/s, %.0f waits/s)"
              % (parties, rounds, parties * rounds, elapsed,
                 rounds / elapsed, parties * rounds / elapsed))

    def test_concurrent_arrival_stress(self):
        """Many threads hammering the barrier with random think time."""
        import random
        parties = 16
        rounds = 300
        bar = Barrier(parties)
        progress = [0] * parties
        errors = []
        rng = random.Random(42)

        def worker(idx):
            try:
                for _ in range(rounds):
                    if rng.random() < 0.1:
                        time.sleep(0.0005)
                    bar.wait()
                    progress[idx] += 1
                    if progress[idx] - min(progress) > 1:
                        errors.append("thread %d lapped" % idx)
                        return
            except Exception as exc:  # pragma: no cover - failure path
                errors.append("thread %d: %r" % (idx, exc))

        start = time.monotonic()
        run_threads(parties, worker, join_timeout=60.0)
        elapsed = time.monotonic() - start

        self.assertEqual(errors, [])
        self.assertEqual(progress, [rounds] * parties)
        print("\n[stress] concurrent-arrival: %d threads x %d rounds in %.2fs"
              % (parties, rounds, elapsed))


class TestExceptionPaths(unittest.TestCase):
    def test_timeout_releases_all_waiters(self):
        """One thread times out -> barrier breaks -> everyone else gets
        BrokenBarrierError instead of blocking forever."""
        parties = 4
        bar = Barrier(parties)
        waiting = parties - 2  # main thread arrives 3rd and times out
        outcomes = [None] * waiting
        t0 = time.monotonic()

        def worker(idx):
            try:
                bar.wait()
                outcomes[idx] = "released"
            except BrokenBarrierError:
                outcomes[idx] = "broken"

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(waiting)]
        for t in threads:
            t.start()
        deadline = time.monotonic() + 5.0
        while bar.n_waiting < waiting and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(bar.n_waiting, waiting)

        # The missing participant "fails" by timing out.
        with self.assertRaises(BarrierTimeoutError):
            bar.wait(timeout=0.2)

        for t in threads:
            t.join(5.0)
        elapsed = time.monotonic() - t0
        self.assertEqual(outcomes, ["broken"] * waiting)
        self.assertLess(elapsed, 5.0, "waiters must be released promptly")
        self.assertTrue(bar.broken)
        self.assertEqual(bar.n_waiting, 0)

    def test_killed_thread_releases_waiters(self):
        """A participant is 'killed' (raises unexpectedly); its cleanup
        path aborts the barrier, releasing the remaining waiters."""
        parties = 5
        bar = Barrier(parties)
        outcomes = [None] * (parties - 1)

        def worker(idx):
            try:
                bar.wait()
                outcomes[idx] = "released"
            except BrokenBarrierError:
                outcomes[idx] = "broken"

        def victim(idx):
            try:
                raise RuntimeError("thread killed")
            except RuntimeError:
                pass  # simulated kill; logged in production
            finally:
                bar.abort()  # cleanup/watchdog path

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(parties - 1)]
        for t in threads:
            t.start()
        deadline = time.monotonic() + 5.0
        while bar.n_waiting < parties - 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(bar.n_waiting, parties - 1)

        killer = threading.Thread(target=victim, args=(parties - 1,))
        killer.start()
        killer.join(5.0)
        for t in threads:
            t.join(5.0)

        self.assertEqual(outcomes, ["broken"] * (parties - 1))
        self.assertTrue(bar.broken)
        self.assertFalse(any(t.is_alive() for t in threads),
                         "no thread may remain blocked")

    def test_latecomer_to_broken_barrier_fails_fast(self):
        bar = Barrier(3)
        bar.abort()
        with self.assertRaises(BrokenBarrierError):
            bar.wait()

    def test_reset_restores_usability(self):
        parties = 3
        bar = Barrier(parties)
        bar.abort()
        self.assertTrue(bar.broken)
        bar.reset()
        self.assertFalse(bar.broken)
        self.assertEqual(bar.n_waiting, 0)

        released = [0]
        lock = threading.Lock()

        def worker(idx):
            bar.wait()
            with lock:
                released[0] += 1

        run_threads(parties, worker)
        self.assertEqual(released[0], parties)
        self.assertEqual(bar.n_waiting, 0)

    def test_exception_inside_wait_does_not_poison_next_round(self):
        """After a timeout break + reset, the next round must still
        require ALL parties (regression test for state residue)."""
        parties = 4
        bar = Barrier(parties)
        with self.assertRaises(BarrierTimeoutError):
            bar.wait(timeout=0.1)
        bar.reset()

        arrivals = [0]
        released_at_arrivals = []
        lock = threading.Lock()

        def worker(idx):
            with lock:
                arrivals[0] += 1
            bar.wait()
            with lock:
                released_at_arrivals.append(arrivals[0])

        run_threads(parties, worker)
        self.assertEqual(sorted(released_at_arrivals), [parties] * parties)


if __name__ == "__main__":
    unittest.main(verbosity=2)
