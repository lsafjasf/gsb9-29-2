"""Self-tests for keywrap: coexistence, interruption, stats, edge cases.

Run:  python3 -m unittest -v test_keywrap
"""

import json
import os
import random
import string
import tempfile
import threading
import unittest

import keywrap
from keywrap import (
    IntegrityError,
    KeyStore,
    KeyWrapError,
    MissingVersionError,
    RecordStore,
    UnknownKeyVersionError,
)


def payload(seed: int, size: int = 64) -> bytes:
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(size))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ks = KeyStore(os.path.join(self.tmp.name, "keys.json"))
        self.v1 = self.ks.create_key()          # v1, active
        self.store = RecordStore(os.path.join(self.tmp.name, "records"))

    def fill(self, n: int, version: int | None = None) -> dict[str, bytes]:
        data = {}
        for i in range(n):
            rid, plain = f"rec-{i:04d}", payload(i)
            self.store.put(rid, keywrap.wrap(plain, self.ks, version=version))
            data[rid] = plain
        return data

    def assert_all_decryptable(self, data: dict[str, bytes]):
        for rid, plain in data.items():
            self.assertEqual(keywrap.unwrap(self.store.get(rid), self.ks), plain,
                             f"record {rid} not decryptable")


# ------------------------------------------------------------------ basics

class TestWrapUnwrap(Base):
    def test_roundtrip(self):
        env = keywrap.wrap(b"hello world", self.ks)
        self.assertEqual(keywrap.unwrap(env, self.ks), b"hello world")

    def test_envelope_carries_version_and_tag(self):
        env = json.loads(keywrap.wrap(b"x", self.ks))
        self.assertEqual(env["v"], self.v1)
        self.assertEqual(env["alg"], keywrap.ALG)
        self.assertTrue(env["nonce"] and env["ct"] and env["tag"])

    def test_empty_data(self):
        env = keywrap.wrap(b"", self.ks)
        self.assertEqual(keywrap.unwrap(env, self.ks), b"")
        self.assertEqual(json.loads(env)["ct"], "")  # empty ciphertext is fine

    def test_binary_data_all_byte_values(self):
        blob = bytes(range(256)) * 4
        self.assertEqual(keywrap.unwrap(keywrap.wrap(blob, self.ks), self.ks), blob)

    def test_nonce_randomized(self):
        a, b = keywrap.wrap(b"same", self.ks), keywrap.wrap(b"same", self.ks)
        self.assertNotEqual(a, b)


# ------------------------------------------------------- version handling

class TestVersioning(Base):
    def test_missing_version_is_an_error_not_a_guess(self):
        env = json.loads(keywrap.wrap(b"secret", self.ks))
        del env["v"]
        blob = json.dumps(env).encode()
        with self.assertRaises(MissingVersionError):
            keywrap.unwrap(blob, self.ks)
        with self.assertRaises(MissingVersionError):
            keywrap.peek_version(blob)

    def test_unknown_version(self):
        env = json.loads(keywrap.wrap(b"secret", self.ks))
        env["v"] = 999
        with self.assertRaises(UnknownKeyVersionError):
            keywrap.unwrap(json.dumps(env).encode(), self.ks)

    def test_version_from_other_keystore_rejected(self):
        env = keywrap.wrap(b"secret", self.ks)
        other = KeyStore()  # empty keystore: knows no versions
        with self.assertRaises(UnknownKeyVersionError):
            keywrap.unwrap(env, other)

    def test_invalid_version_type(self):
        env = json.loads(keywrap.wrap(b"secret", self.ks))
        env["v"] = "1"  # string instead of int
        with self.assertRaises(KeyWrapError):
            keywrap.unwrap(json.dumps(env).encode(), self.ks)

    def test_malformed_envelope(self):
        with self.assertRaises(KeyWrapError):
            keywrap.unwrap(b"not json at all", self.ks)
        with self.assertRaises(KeyWrapError):
            keywrap.unwrap(b"[1,2,3]", self.ks)

    def test_tampered_ciphertext_detected(self):
        env = json.loads(keywrap.wrap(b"secret", self.ks))
        import base64
        ct = bytearray(base64.b64decode(env["ct"]))
        ct[0] ^= 0x01
        env["ct"] = base64.b64encode(bytes(ct)).decode()
        with self.assertRaises(IntegrityError):
            keywrap.unwrap(json.dumps(env).encode(), self.ks)

    def test_tampered_version_detected(self):
        # version is MAC-bound: swapping to another *existing* version fails
        self.ks.create_key()  # v2
        env = json.loads(keywrap.wrap(b"secret", self.ks, version=self.v1))
        env["v"] = 2
        with self.assertRaises(IntegrityError):
            keywrap.unwrap(json.dumps(env).encode(), self.ks)


# ------------------------------------------------- coexistence + rotation

class TestRotation(Base):
    def test_coexistence_old_and_new_both_decrypt(self):
        old_data = self.fill(10)                      # written under v1
        v2 = keywrap.begin_rotation(self.ks)          # activate v2
        new_plain = b"written after rotation began"
        self.store.put("rec-new", keywrap.wrap(new_plain, self.ks))
        # mixed dataset: v1 records + v2 record, all readable
        self.assert_all_decryptable(old_data)
        self.assertEqual(keywrap.unwrap(self.store.get("rec-new"), self.ks), new_plain)
        counts = keywrap.count_by_version(self.store)
        self.assertEqual(counts, {self.v1: 10, v2: 1})

    def test_full_rotation_and_safe_retirement(self):
        data = self.fill(25)
        v2 = keywrap.begin_rotation(self.ks)
        stats = keywrap.rotate(self.store, self.ks)
        self.assertFalse(stats.interrupted)
        self.assertEqual(stats.reencrypted, 25)
        self.assertEqual(stats.failed, 0)
        self.assert_all_decryptable(data)
        # old key can only be retired once nothing references it
        self.assertEqual(keywrap.remaining_on_version(self.store, self.v1), 0)
        keywrap.retire_key(self.ks, self.store, self.v1)
        self.assertEqual(self.ks.versions(), [v2])
        self.assert_all_decryptable(data)  # still fine: everything is v2

    def test_retire_refused_while_old_records_remain(self):
        self.fill(5)
        keywrap.begin_rotation(self.ks)
        with self.assertRaises(KeyWrapError) as ctx:
            keywrap.retire_key(self.ks, self.store, self.v1)
        self.assertIn("5 record(s)", str(ctx.exception))

    def test_rotation_stats_and_progress(self):
        self.fill(10)                                  # v1
        v2 = keywrap.begin_rotation(self.ks)
        for i in range(10, 14):                        # new writes land on v2
            self.store.put(f"rec-{i:04d}", keywrap.wrap(payload(i), self.ks))
        progress = []
        stats = keywrap.rotate(
            self.store, self.ks,
            on_progress=lambda s, done, rid: progress.append((done, rid)),
        )
        self.assertEqual(stats.total, 14)
        self.assertEqual(stats.reencrypted, 10)        # only v1 records moved
        self.assertEqual(stats.already_current, 4)     # v2 records skipped
        self.assertEqual(stats.bytes_reencrypted, 10 * 64)
        self.assertEqual(len(progress), 14)            # progress per record
        self.assertEqual(keywrap.count_by_version(self.store), {v2: 14})


# ------------------------------------------------- interruption & resume

class TestInterruption(Base):
    def make_stopper(self, stop_after: int):
        state = {"seen": 0}
        def should_stop():
            state["seen"] += 1
            return state["seen"] > stop_after
        return should_stop

    def test_interrupt_mid_rotation_then_resume(self):
        data = self.fill(30)
        v2 = keywrap.begin_rotation(self.ks)

        # crash after 7 records
        stats1 = keywrap.rotate(self.store, self.ks,
                                should_stop=self.make_stopper(7))
        self.assertTrue(stats1.interrupted)
        self.assertEqual(stats1.reencrypted, 7)
        self.assertEqual(stats1.remaining, 30 - 7)

        # interruption point: mixed v1/v2 dataset, EVERYTHING still decrypts
        self.assert_all_decryptable(data)
        counts = keywrap.count_by_version(self.store)
        self.assertEqual(counts[self.v1] + counts[v2], 30)

        # old key must NOT be retired at this point
        with self.assertRaises(KeyWrapError):
            keywrap.retire_key(self.ks, self.store, self.v1)

        # resume: picks up where it stopped, skips already-migrated records
        stats2 = keywrap.rotate(self.store, self.ks)
        self.assertFalse(stats2.interrupted)
        self.assertEqual(stats2.reencrypted, 30 - 7)
        self.assertEqual(stats2.already_current, 7)
        self.assert_all_decryptable(data)
        self.assertEqual(keywrap.remaining_on_version(self.store, self.v1), 0)

    def test_interrupt_at_every_boundary(self):
        """Interrupt after k records for every k: data is always readable."""
        for stop_after in (0, 1, 5, 9):
            with self.subTest(stop_after=stop_after):
                tmp = tempfile.TemporaryDirectory()
                ks = KeyStore()
                ks.create_key()
                store = RecordStore(os.path.join(tmp.name, "r"))
                data = {}
                for i in range(10):
                    rid, plain = f"rec-{i}", payload(i)
                    store.put(rid, keywrap.wrap(plain, ks))
                    data[rid] = plain
                keywrap.begin_rotation(ks)
                stats = keywrap.rotate(store, ks,
                                       should_stop=self.make_stopper(stop_after))
                self.assertTrue(stats.interrupted)
                for rid, plain in data.items():
                    self.assertEqual(keywrap.unwrap(store.get(rid), ks), plain)
                tmp.cleanup()

    def test_interrupt_between_phases(self):
        data = self.fill(8)
        # crash right after phase 1 (new key active, nothing re-encrypted)
        v2 = keywrap.begin_rotation(self.ks)
        self.assert_all_decryptable(data)
        self.assertEqual(keywrap.count_by_version(self.store), {self.v1: 8})
        # crash right after phase 2, before phase 3 (retire)
        keywrap.rotate(self.store, self.ks)
        self.assert_all_decryptable(data)
        self.assertTrue(self.ks.has(self.v1))  # old key still around
        # phase 3 completes the cycle
        keywrap.retire_key(self.ks, self.store, self.v1)
        self.assert_all_decryptable(data)

    def test_keystore_survives_restart(self):
        self.fill(3)
        keywrap.begin_rotation(self.ks)
        # simulate process restart: reload keystore from disk
        ks2 = KeyStore(os.path.join(self.tmp.name, "keys.json"))
        self.assertEqual(ks2.versions(), self.ks.versions())
        self.assertEqual(ks2.active_version, self.ks.active_version)
        stats = keywrap.rotate(self.store, ks2)
        self.assertEqual(stats.reencrypted, 3)


# ------------------------------------------------------- premature delete

class TestPrematureKeyDeletion(Base):
    def test_deleted_old_key_breaks_old_records_loudly(self):
        data = self.fill(6)
        v2 = keywrap.begin_rotation(self.ks)
        self.ks.retire(self.v1)  # forced low-level delete, old data remains
        for rid in data:
            with self.assertRaises(UnknownKeyVersionError):
                keywrap.unwrap(self.store.get(rid), self.ks)
        # rotation can no longer save those records: they count as failed
        stats = keywrap.rotate(self.store, self.ks, target_version=v2)
        self.assertEqual(stats.failed, 6)
        self.assertEqual(stats.reencrypted, 0)

    def test_cannot_retire_active_key(self):
        with self.assertRaises(KeyWrapError):
            self.ks.retire(self.v1)


# ------------------------------------------------------------- concurrency

class TestConcurrency(Base):
    def test_concurrent_read_write_during_rotation(self):
        data = self.fill(40)
        keywrap.begin_rotation(self.ks)
        errors: list[BaseException] = []
        stop = threading.Event()

        def reader():
            rng = random.Random()
            ids = list(data)
            while not stop.is_set():
                rid = rng.choice(ids)
                try:
                    got = keywrap.unwrap(self.store.get(rid), self.ks)
                    if got != data[rid]:
                        errors.append(AssertionError(f"{rid}: content mismatch"))
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

        def writer(n):
            rng = random.Random(n)
            alphabet = string.ascii_lowercase
            try:
                for i in range(15):
                    rid = f"new-{n}-{i}"
                    plain = "".join(rng.choice(alphabet) for _ in range(32)).encode()
                    self.store.put(rid, keywrap.wrap(plain, self.ks))
                    if keywrap.unwrap(self.store.get(rid), self.ks) != plain:
                        errors.append(AssertionError(f"{rid}: write-read mismatch"))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        threads += [threading.Thread(target=writer, args=(n,)) for n in range(2)]
        for t in threads:
            t.start()
        stats = keywrap.rotate(self.store, self.ks)  # rotation runs concurrently
        stop.set()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(stats.failed, 0)
        self.assert_all_decryptable(data)
        # every record (old, rotated, newly written) ends on the active version
        counts = keywrap.count_by_version(self.store)
        self.assertEqual(list(counts), [self.ks.active_version])
        self.assertEqual(sum(counts.values()), 40 + 2 * 15)


if __name__ == "__main__":
    unittest.main()
