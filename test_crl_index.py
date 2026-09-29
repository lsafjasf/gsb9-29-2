"""CRLIndex 自测：空列表、重复条目、超大列表、更新中断、新鲜度、签发方、原子性。"""
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

from crl_index import (
    CRLFormatError,
    CRLIndex,
    IssuerMismatchError,
    NotYetValidError,
    StaleCRLError,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)
ISSUER = "CN=Test CA, O=Example"


def fresh_index(serials=(), issuer=ISSUER):
    idx = CRLIndex(issuer)
    idx.load(
        serials,
        this_update=NOW - timedelta(hours=1),
        next_update=NOW + timedelta(hours=24),
    )
    return idx


class TestBasicQuery(unittest.TestCase):
    def test_hit_and_miss(self):
        idx = fresh_index([100, "0x64", 200])  # 100 与 0x64 是同一条
        self.assertTrue(idx.is_revoked(100, now=NOW))
        self.assertTrue(idx.is_revoked("0xC8", now=NOW))  # 200
        self.assertFalse(idx.is_revoked(999, now=NOW))

    def test_serial_formats(self):
        idx = fresh_index(["255"])
        self.assertTrue(idx.is_revoked(255, now=NOW))
        self.assertTrue(idx.is_revoked("0xff", now=NOW))
        self.assertTrue(idx.is_revoked("255", now=NOW))


class TestEmptyList(unittest.TestCase):
    def test_empty_list_queries_all_miss(self):
        idx = fresh_index([])
        self.assertEqual(idx.status()["count"], 0)
        self.assertFalse(idx.is_revoked(1, now=NOW))
        self.assertFalse(idx.is_revoked(2**128, now=NOW))

    def test_never_loaded_rejects_query(self):
        # 空索引（从未加载）不能当成"全部有效"
        idx = CRLIndex(ISSUER)
        with self.assertRaises(NotYetValidError):
            idx.is_revoked(1, now=NOW)

    def test_empty_file_rejected(self):
        idx = CRLIndex(ISSUER)
        with self.assertRaises(CRLFormatError):
            idx.load_stream(iter([]))


class TestDuplicates(unittest.TestCase):
    def test_duplicates_deduped(self):
        idx = fresh_index([1, 2, 2, "0x2", 3, 3, 3])
        self.assertEqual(idx.status()["count"], 3)
        self.assertTrue(idx.is_revoked(2, now=NOW))

    def test_incremental_add_existing_is_idempotent(self):
        idx = fresh_index([1, 2])
        idx.update(add=[2, 2, 3])
        self.assertEqual(idx.status()["count"], 3)


class TestFreshness(unittest.TestCase):
    def test_expired_list_rejected(self):
        idx = fresh_index([1])
        future = NOW + timedelta(hours=25)  # 超过 next_update
        with self.assertRaises(StaleCRLError):
            idx.is_revoked(1, now=future)

    def test_not_yet_valid_rejected(self):
        idx = fresh_index([1])
        past = NOW - timedelta(hours=2)  # 早于 this_update
        with self.assertRaises(NotYetValidError):
            idx.is_revoked(1, now=past)

    def test_boundary_instants_accepted(self):
        idx = fresh_index([1])
        self.assertTrue(idx.is_revoked(1, now=NOW - timedelta(hours=1)))  # == this_update
        self.assertTrue(idx.is_revoked(1, now=NOW + timedelta(hours=24)))  # == next_update

    def test_update_extends_validity(self):
        idx = fresh_index([1])
        idx.update(next_update=NOW + timedelta(days=7))
        self.assertTrue(idx.is_revoked(1, now=NOW + timedelta(days=2)))


class TestIssuer(unittest.TestCase):
    def test_load_with_wrong_issuer_rejected(self):
        idx = CRLIndex(ISSUER)
        with self.assertRaises(IssuerMismatchError):
            idx.load([1], this_update=NOW, next_update=None, issuer="CN=Other CA")

    def test_query_with_wrong_issuer_rejected(self):
        idx = fresh_index([1])
        with self.assertRaises(IssuerMismatchError):
            idx.is_revoked(1, issuer="CN=Other CA", now=NOW)

    def test_file_header_issuer_checked(self):
        idx = CRLIndex(ISSUER)
        lines = [
            '{"issuer": "CN=Other CA", "thisUpdate": "2026-09-30T00:00:00Z", "nextUpdate": null}',
            "1",
        ]
        with self.assertRaises(IssuerMismatchError):
            idx.load_stream(iter(lines))


class TestInterruptedUpdate(unittest.TestCase):
    def test_truncated_file_keeps_old_snapshot(self):
        idx = fresh_index([1, 2, 3])
        before = idx.status()
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write('{"issuer": "%s", "thisUpdate": "2026-09-30T11:00:00Z", "nextUpdate": "2026-10-01T12:00:00Z"}\n' % ISSUER)
            fh.write("100\n200\n")
            fh.write("这不是序列号\n")  # 模拟传输中断/文件损坏
            path = fh.name
        try:
            with self.assertRaises(CRLFormatError):
                idx.load_file(path)
        finally:
            os.unlink(path)
        # 旧快照完整保留：版本、数量、查询结果都不变
        self.assertEqual(idx.status(), before)
        self.assertTrue(idx.is_revoked(2, now=NOW))
        self.assertFalse(idx.is_revoked(100, now=NOW))

    def test_generator_exception_keeps_old_snapshot(self):
        idx = fresh_index([7])
        before = idx.status()

        def broken():
            yield '{"issuer": "%s", "thisUpdate": "2026-09-30T11:00:00Z", "nextUpdate": null}' % ISSUER
            yield "42"
            raise ConnectionError("网络中断")

        with self.assertRaises(ConnectionError):
            idx.load_stream(broken())
        self.assertEqual(idx.status(), before)
        self.assertTrue(idx.is_revoked(7, now=NOW))
        self.assertFalse(idx.is_revoked(42, now=NOW))

    def test_failed_incremental_update_keeps_old_snapshot(self):
        idx = fresh_index([1])
        before = idx.status()
        with self.assertRaises(Exception):
            idx.update(add=[object()])  # 无法识别的类型，构建中途失败
        self.assertEqual(idx.status(), before)


class TestAtomicity(unittest.TestCase):
    def test_concurrent_readers_never_see_partial_list(self):
        old_set = frozenset(range(0, 5000, 2))    # 2500 条偶数
        new_set = frozenset(range(1, 5001, 2))    # 2500 条奇数
        idx = fresh_index(old_set)
        stop = threading.Event()
        errors = []

        def reader():
            while not stop.is_set():
                snap = idx._snapshot
                # 一致性断言：任何时刻看到的必须完整等于旧版或新版
                if snap.serials != old_set and snap.serials != new_set:
                    errors.append(f"看到半份列表: count={snap.count}")
                    return
                # 同一份快照内部必须自洽：成员判定与集合内容一致
                probe = 0 if snap.serials == old_set else 1
                if (probe in snap.serials) is not True or ((probe + 1) in snap.serials):
                    errors.append("快照内部不一致")
                    return

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for _ in range(200):
            idx.load(old_set, this_update=NOW - timedelta(hours=1), next_update=NOW + timedelta(hours=24))
            idx.load(new_set, this_update=NOW - timedelta(hours=1), next_update=NOW + timedelta(hours=24))
        stop.set()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


class TestHugeList(unittest.TestCase):
    def test_million_entries(self):
        n = 1_000_000
        idx = CRLIndex(ISSUER)
        count = idx.load(
            range(n),
            this_update=NOW - timedelta(hours=1),
            next_update=NOW + timedelta(hours=24),
        )
        self.assertEqual(count, n)
        self.assertTrue(idx.is_revoked(0, now=NOW))
        self.assertTrue(idx.is_revoked(n - 1, now=NOW))
        self.assertTrue(idx.is_revoked(n // 2, now=NOW))
        self.assertFalse(idx.is_revoked(n, now=NOW))
        self.assertFalse(idx.is_revoked(2 * n, now=NOW))
        # 增量更新在超大列表上同样生效
        idx.update(add=[n], remove=[0])
        self.assertFalse(idx.is_revoked(0, now=NOW))
        self.assertTrue(idx.is_revoked(n, now=NOW))


if __name__ == "__main__":
    unittest.main(verbosity=2)
