"""Self-tests for stm.py. Run: python3 test_stm.py"""

import threading
import time
import unittest

from stm import STM, AbortTransaction, CommitFailure


def force_conflict(stm, slow_body, timeout=2):
    """Run slow_body on the current thread inside stm.run, after spawning a
    thread that commits a conflicting update while slow_body is between its
    read and commit. slow_body receives the 'entered' event to signal after
    its reads, and a 'release' event it waits on before committing.

    Returns (raised,): the caller asserts CommitFailure with retries=0.
    """
    entered = threading.Event()
    release = threading.Event()

    def interloper():
        entered.wait(timeout)
        stm.run(lambda tx: tx.set("k", tx.get("k", 0) + 1))
        release.set()

    th = threading.Thread(target=interloper)
    th.start()

    def body(tx):
        slow_body(tx, entered)
        release.wait(timeout)

    try:
        stm.run(body, retries=0)
        raised = False
    except CommitFailure:
        raised = True
    th.join()
    return raised


class TestSingleTransaction(unittest.TestCase):
    def test_commit_visible_and_versioned(self):
        stm = STM()
        stm.run(lambda tx: (tx.set("a", 1), tx.set("b", 2)))
        self.assertEqual(stm.snapshot(), {"a": 1, "b": 2})
        stm.run(lambda tx: tx.set("a", tx.get("a") + 10))
        self.assertEqual(stm.get_committed("a"), 11)

    def test_read_absent_key_then_insert_commits(self):
        stm = STM()
        stm.run(lambda tx: tx.set("x", tx.get("x", 0) + 1))
        self.assertEqual(stm.get_committed("x"), 1)

    def test_exception_rolls_back_everything(self):
        stm = STM()
        stm.run(lambda tx: tx.set("keep", 1))

        def body(tx):
            tx.set("keep", 99)
            tx.set("temp", 2)
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            stm.run(body)
        self.assertEqual(stm.get_committed("keep"), 1)
        self.assertIsNone(stm.get_committed("temp"))

    def test_explicit_abort_discards_all_no_retry(self):
        stm = STM()
        stm.run(lambda tx: tx.set("a", 1))

        def body(tx):
            tx.set("a", 2)
            tx.abort()

        self.assertIsNone(stm.run(body))
        self.assertEqual(stm.get_committed("a"), 1)
        self.assertEqual(stm.stats()["retries"], 0)


class TestRollbackOnConflict(unittest.TestCase):
    def test_conflict_rolls_back_entire_write_set(self):
        """Deterministic OCC conflict: T reads k while it is version 1 and
        stages multiple writes; another thread bumps k before T commits.
        Validation must fail and NONE of T's writes may become visible."""
        stm = STM()
        stm.run(lambda tx: tx.set("k", 0))

        def slow_body(tx, entered):
            value = tx.get("k")
            tx.set("k", value + 10)
            tx.set("side_effect", value + 100)
            self.assertEqual(tx.get("side_effect"), 100)  # read-your-writes
            entered.set()

        raised = force_conflict(stm, slow_body)
        self.assertTrue(raised, "expected CommitFailure after validation loss")

        # ASSERTION: whole write set rolled back, no partial commit
        self.assertEqual(stm.get_committed("k"), 1)
        self.assertIsNone(stm.get_committed("side_effect"))
        stats = stm.stats()
        self.assertEqual(stats["commits"], 2)   # initial + interloper
        self.assertEqual(stats["conflicts"], 1)
        self.assertEqual(stats["retries"], 0)

    def test_absent_key_insert_conflict_rolls_back(self):
        stm = STM()

        def slow_body(tx, entered):
            value = tx.get("new", 0)
            tx.set("new", value + 1)
            tx.set("other", "x")
            entered.set()

        # interloper inserts "k", not "new" -> no conflict, commit succeeds;
        # then repeat with interloper inserting "new" -> conflict.
        entered = threading.Event()
        release = threading.Event()

        def interloper():
            entered.wait(2)
            stm.run(lambda tx: tx.set("new", 42))
            release.set()

        th = threading.Thread(target=interloper)
        th.start()

        def body(tx):
            slow_body(tx, entered)
            release.wait(2)

        with self.assertRaises(CommitFailure):
            stm.run(body, retries=0)
        th.join()
        self.assertEqual(stm.get_committed("new"), 42)
        self.assertIsNone(stm.get_committed("other"))

    def test_rmw_on_hot_key_always_conflicts_and_repairs(self):
        """Two read-modify-write txns started at the same version cannot both
        commit; the loser retries until it succeeds (no lost update)."""
        stm = STM()
        stm.run(lambda tx: tx.set("k", 0))
        barrier = threading.Event()

        def body(first):
            def txn(tx):
                if first:
                    barrier.set()
                else:
                    barrier.wait(2)
                value = tx.get("k")
                time.sleep(0.02)
                tx.set("k", value + 1)
            stm.run(txn, retries=100)

        t1 = threading.Thread(target=body, args=(True,))
        t2 = threading.Thread(target=body, args=(False,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(stm.get_committed("k"), 2)
        self.assertGreaterEqual(stm.stats()["conflicts"], 1)


class TestNested(unittest.TestCase):
    def test_inner_abort_keeps_outer_changes(self):
        stm = STM()

        def body(tx):
            tx.set("outer", 1)
            with stm.savepoint():
                tx.set("inner", 2)
                tx.set("outer", 99)
                tx.abort()
            tx.set("after", 3)

        stm.run(body)
        self.assertEqual(stm.snapshot(), {"outer": 1, "after": 3})

    def test_inner_commits_merge_into_outer(self):
        stm = STM()

        def body(tx):
            tx.set("a", 1)
            with stm.savepoint():
                tx.set("b", tx.get("a") + 1)
                with stm.savepoint():
                    tx.set("c", tx.get("b") + 1)

        stm.run(body)
        self.assertEqual(stm.snapshot(), {"a": 1, "b": 2, "c": 3})

    def test_outer_abort_after_inner_commit_discards_all(self):
        stm = STM()

        def body(tx):
            with stm.savepoint():
                tx.set("a", 1)
            tx.abort()

        self.assertIsNone(stm.run(body))
        self.assertEqual(stm.snapshot(), {})

    def test_inner_exception_discards_only_inner_frame(self):
        stm = STM()

        def body(tx):
            tx.set("a", 1)
            try:
                with stm.savepoint():
                    tx.set("b", 2)
                    raise RuntimeError("inner failure")
            except RuntimeError:
                pass
            tx.set("c", 3)

        stm.run(body)
        self.assertEqual(stm.snapshot(), {"a": 1, "c": 3})

    def test_inner_abort_drops_reads_that_were_speculative(self):
        stm = STM()
        stm.run(lambda tx: tx.set("k", 0))
        seen = {}

        def body(tx):
            with stm.savepoint():
                seen["v"] = tx.get("k")
                tx.abort()
            tx.set("k", tx.get("k") + 1)

        stm.run(body)
        self.assertEqual(stm.snapshot(), {"k": 1})


class TestConcurrency(unittest.TestCase):
    def test_disjoint_keys_commit_without_conflict(self):
        stm = STM()
        n_threads, n_iters = 8, 300

        def worker(i):
            key = "k%d" % i
            for _ in range(n_iters):
                stm.run(lambda tx: tx.set(key, tx.get(key, 0) + 1))

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for i in range(n_threads):
            self.assertEqual(stm.get_committed("k%d" % i), n_iters)
        self.assertEqual(stm.stats()["conflicts"], 0)

    def test_hotspot_counter_no_lost_updates(self):
        stm = STM()
        stm.run(lambda tx: tx.set("hot", 0))
        n_threads, n_iters = 8, 100

        def body(tx):
            tx.set("hot", tx.get("hot") + 1)
            time.sleep(0.0005)  # widen critical window to force conflicts

        def worker(_):
            for _ in range(n_iters):
                stm.run(body, retries=1000)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(stm.get_committed("hot"), n_threads * n_iters)
        self.assertGreater(stm.stats()["conflicts"], 0)

    def test_retry_eventually_succeeds(self):
        stm = STM()
        stm.run(lambda tx: tx.set("k", 0))
        attempts = {"n": 0}

        def body(tx):
            attempts["n"] += 1
            value = tx.get("k")          # read first (stale on attempt 1)
            if attempts["n"] == 1:
                def interloper():
                    stm.run(lambda t: t.set("k", t.get("k") + 1))
                th = threading.Thread(target=interloper)
                th.start()
                th.join()
            tx.set("k", value + 1)

        stm.run(body, retries=5)
        self.assertEqual(stm.get_committed("k"), 2)
        self.assertGreaterEqual(attempts["n"], 2)

    def test_retry_budget_exhausted(self):
        stm = STM()
        stm.run(lambda tx: tx.set("k", 0))
        stop = threading.Event()

        def bully():
            while not stop.is_set():
                stm.run(lambda tx: tx.set("k", tx.get("k") + 1), retries=1000)
                time.sleep(0.0002)

        th = threading.Thread(target=bully)
        th.start()
        try:
            def body(tx):
                value = tx.get("k")
                time.sleep(0.01)
                tx.set("k", value + 1)

            with self.assertRaises(CommitFailure):
                stm.run(body, retries=1)
        finally:
            stop.set()
            th.join()
        stats = stm.stats()
        self.assertGreaterEqual(stats["conflicts"], 1)
        # every attempt ends in either a commit or a conflict
        self.assertEqual(stats["attempts"], stats["commits"] + stats["conflicts"])
        # exactly one run exhausted its budget: conflicts exceed retries by
        # the number of failed runs (the winner's internal retries balance)
        self.assertGreaterEqual(stats["conflicts"] - stats["retries"], 1)

    def test_transfer_invariant_preserved(self):
        stm = STM()
        stm.run(lambda tx: (tx.set("a", 1000), tx.set("b", 1000)))

        def out(tx):
            if tx.get("a") >= 7:
                tx.set("a", tx.get("a") - 7)
                tx.set("b", tx.get("b") + 7)

        def back(tx):
            if tx.get("b") >= 7:
                tx.set("b", tx.get("b") - 7)
                tx.set("a", tx.get("a") + 7)

        def worker(_):
            for _ in range(150):
                stm.run(out, retries=1000)
                stm.run(back, retries=1000)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        total = stm.get_committed("a") + stm.get_committed("b")
        self.assertEqual(total, 2000)


class TestBoundary(unittest.TestCase):
    def test_ops_outside_transaction_rejected(self):
        stm = STM()
        with self.assertRaises(RuntimeError):
            stm.tx.get("x")
        with self.assertRaises(RuntimeError):
            stm.tx.set("x", 1)

    def test_run_cannot_be_nested(self):
        stm = STM()

        def outer(tx):
            tx.set("a", 1)
            stm.run(lambda inner: inner.set("b", 2))

        with self.assertRaises(RuntimeError):
            stm.run(outer)
        self.assertEqual(stm.snapshot(), {})

    def test_savepoint_requires_transaction(self):
        stm = STM()
        with self.assertRaises(RuntimeError):
            with stm.savepoint():
                pass

    def test_aborted_insert_rolled_back(self):
        stm = STM()

        def slow_body(tx, entered):
            current = tx.get("k", 0)
            tx.set("k", current + 10)
            tx.set("new", 1)
            entered.set()

        raised = force_conflict(stm, slow_body)
        self.assertTrue(raised)
        self.assertEqual(stm.snapshot(), {"k": 1})

    def test_zero_retries_single_attempt(self):
        stm = STM()
        stm.run(lambda tx: tx.set("k", 0))

        def slow_body(tx, entered):
            tx.set("k", tx.get("k") + 1)
            entered.set()

        raised = force_conflict(stm, slow_body)
        self.assertTrue(raised)
        stats = stm.stats()
        self.assertEqual(stats["retries"], 0)
        self.assertEqual(stats["conflicts"], 1)
        self.assertEqual(stats["commits"], 2)

    def test_stats_reset(self):
        stm = STM()
        stm.run(lambda tx: tx.set("a", 1))
        stm.reset_stats()
        stats = stm.stats()
        self.assertEqual(stats["attempts"], 0)
        self.assertEqual(stats["conflict_rate"], 0.0)
        self.assertEqual(stats["commit_latency"]["count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
