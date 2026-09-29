"""crl_store 自测: python3 test_crl_store.py -v"""

import threading
import unittest
from datetime import datetime, timedelta, timezone

from crl_store import (
    BadSerialError,
    CRLExpiredError,
    CRLNotYetValidError,
    CRLStore,
    IssuerMismatchError,
    NoCRLLoadedError,
    normalize_serial,
)

NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
ISSUER = "CN=Unit Test CA"


def make_store(serials=(), issuer=ISSUER, now=NOW, valid_days=7):
    store = CRLStore(issuer=issuer, clock=lambda: now)
    if serials is not None:
        store.load(
            serials,
            this_update=NOW - timedelta(days=1),
            next_update=NOW + timedelta(days=valid_days),
        )
    return store


class TestSerialNormalization(unittest.TestCase):
    def test_hex_and_int_equivalent(self):
        self.assertEqual(normalize_serial("0A1B"), normalize_serial(0x0A1B))
        self.assertEqual(normalize_serial("0x0a:1b"), 0x0A1B)
        self.assertEqual(normalize_serial("ff"), 255)

    def test_bad_serial(self):
        for bad in (-1, "", "zz", None, True):
            with self.assertRaises(BadSerialError, msg=repr(bad)):
                normalize_serial(bad)


class TestQuery(unittest.TestCase):
    def test_hit_and_miss(self):
        store = make_store(["0a", "0B", 255])
        self.assertTrue(store.is_revoked("0x0A"))
        self.assertTrue(store.is_revoked(11))
        self.assertTrue(store.is_revoked("ff"))
        self.assertFalse(store.is_revoked("deadbeef"))

    def test_empty_list(self):
        store = make_store([])
        self.assertFalse(store.is_revoked("01"))
        self.assertEqual(len(store.current()), 0)

    def test_duplicates_deduped(self):
        store = make_store(["0a", "0A", "0x0a", 10, 10])
        self.assertEqual(len(store.current()), 1)
        self.assertTrue(store.is_revoked(10))

    def test_no_list_loaded(self):
        store = CRLStore(issuer=ISSUER, clock=lambda: NOW)
        with self.assertRaises(NoCRLLoadedError):
            store.is_revoked("01")


class TestFreshness(unittest.TestCase):
    def test_expired_list_rejected(self):
        store = make_store(["0a"], now=NOW + timedelta(days=8))
        with self.assertRaises(CRLExpiredError) as ctx:
            store.is_revoked("0a")
        self.assertIn("过期", str(ctx.exception))

    def test_not_yet_valid_rejected(self):
        store = make_store(["0a"], now=NOW - timedelta(days=2))
        with self.assertRaises(CRLNotYetValidError):
            store.is_revoked("0a")

    def test_issuer_mismatch_rejected(self):
        store = make_store(["0a"])
        with self.assertRaises(IssuerMismatchError) as ctx:
            store.is_revoked("0a", issuer="CN=Other CA")
        self.assertIn("不匹配", str(ctx.exception))

    def test_issuer_match_accepted(self):
        store = make_store(["0a"])
        self.assertTrue(store.is_revoked("0a", issuer=ISSUER))


class TestIncrementalUpdate(unittest.TestCase):
    def test_add_and_remove(self):
        store = make_store(["01", "02"])
        store.apply_delta(add=["03"], remove=["01"])
        self.assertFalse(store.is_revoked("01"))
        self.assertTrue(store.is_revoked("02"))
        self.assertTrue(store.is_revoked("03"))

    def test_delta_refreshes_validity(self):
        store = make_store(["01"])
        new_next = NOW + timedelta(days=30)
        store.apply_delta(next_update=new_next)
        self.assertEqual(store.current().next_update, new_next)

    def test_delta_requires_loaded_list(self):
        store = CRLStore(issuer=ISSUER, clock=lambda: NOW)
        with self.assertRaises(NoCRLLoadedError):
            store.apply_delta(add=["01"])


class TestAtomicUpdate(unittest.TestCase):
    def test_interrupted_load_keeps_old_list(self):
        store = make_store(["aa"])

        def broken_source():
            yield "bb"
            raise ConnectionError("下载中断")

        with self.assertRaises(ConnectionError):
            store.load(broken_source(), NOW, NOW + timedelta(days=7))
        # 旧列表完好, 新数据不可见
        self.assertTrue(store.is_revoked("aa"))
        self.assertFalse(store.is_revoked("bb"))
        self.assertEqual(len(store.current()), 1)

    def test_interrupted_delta_keeps_old_list(self):
        store = make_store(["aa"])

        def broken_add():
            yield "bb"
            raise RuntimeError("增量包损坏")

        with self.assertRaises(RuntimeError):
            store.apply_delta(add=broken_add())
        self.assertEqual(len(store.current()), 1)
        self.assertTrue(store.is_revoked("aa"))

    def test_concurrent_readers_never_see_partial_list(self):
        # 交替发布两份内容互斥的列表; 读者观察到的任何快照都必须完整属于其中一份
        list_a = [f"{i:06x}" for i in range(5000)]
        list_b = [f"{i:06x}" for i in range(5000, 10000)]
        store = make_store(list_a)
        stop = threading.Event()
        failures = []

        def writer():
            flip = False
            while not stop.is_set():
                serials = list_b if flip else list_a
                store.load(serials, NOW - timedelta(days=1), NOW + timedelta(days=7))
                flip = not flip

        def reader():
            while not stop.is_set():
                crl = store.current()
                sample_a = f"{0:06x}" in crl
                sample_b = f"{9999:06x}" in crl
                if sample_a == sample_b:  # 两份互斥, 同时命中/同时落空即读到半份
                    failures.append((sample_a, sample_b))
                    return

        threads = [threading.Thread(target=writer)] + [
            threading.Thread(target=reader) for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads[1:]:
            t.join(timeout=10)
        stop.set()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(failures, [])


class TestLargeList(unittest.TestCase):
    def test_large_list_load_and_query(self):
        n = 500_000
        store = make_store(range(n))
        self.assertEqual(len(store.current()), n)
        self.assertTrue(store.is_revoked(n - 1))
        self.assertFalse(store.is_revoked(n))


class TestFileLoading(unittest.TestCase):
    def test_load_from_file(self):
        import os, tempfile

        content = (
            "# test crl\n"
            f"issuer: {ISSUER}\n"
            f"this_update: {(NOW - timedelta(days=1)).isoformat()}\n"
            f"next_update: {(NOW + timedelta(days=7)).isoformat()}\n"
            "0a\n0B\nff:00\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".crl", delete=False) as fh:
            fh.write(content)
            path = fh.name
        try:
            store = CRLStore(clock=lambda: NOW)
            store.load_from_file(path)
            self.assertTrue(store.is_revoked("0x0A", issuer=ISSUER))
            self.assertTrue(store.is_revoked("ff00", issuer=ISSUER))
            self.assertFalse(store.is_revoked("01", issuer=ISSUER))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
