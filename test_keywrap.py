import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

import keywrap
from keywrap import (
    IntegrityError,
    EnvelopeFormatError,
    KeyRetirementError,
    KeyRing,
    MissingVersionError,
    RotationStateError,
    UnknownVersionError,
)


class SimulatedCrash(Exception):
    pass


class KeyWrapTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.record_dir = self.dir / "records"
        self.record_dir.mkdir()
        self.ring = KeyRing.create(self.dir / "keyring.json")

    def write_record(self, name, envelope):
        path = self.record_dir / name
        path.write_bytes(envelope)
        return path

    def write_records(self, count, prefix="rec"):
        payloads = {}
        for i in range(count):
            plaintext = f"{prefix}-payload-{i}".encode()
            self.write_record(f"{prefix}-{i}{keywrap.RECORD_SUFFIX}", keywrap.wrap(self.ring, plaintext))
            payloads[f"{prefix}-{i}{keywrap.RECORD_SUFFIX}"] = plaintext
        return payloads

    def assert_all_records_decrypt(self, payloads):
        for name, plaintext in payloads.items():
            got = keywrap.unwrap(self.ring, (self.record_dir / name).read_bytes())
            self.assertEqual(got, plaintext, name)


class TestWrapUnwrap(KeyWrapTestCase):
    def test_roundtrip(self):
        envelope = keywrap.wrap(self.ring, b"hello")
        self.assertEqual(keywrap.unwrap(self.ring, envelope), b"hello")

    def test_empty_data(self):
        envelope = keywrap.wrap(self.ring, b"")
        obj = json.loads(envelope)
        self.assertEqual(obj["ct"], "")
        self.assertEqual(keywrap.unwrap(self.ring, envelope), b"")

    def test_binary_data(self):
        plaintext = os.urandom(4096)
        envelope = keywrap.wrap(self.ring, plaintext)
        self.assertEqual(keywrap.unwrap(self.ring, envelope), plaintext)

    def test_missing_version_refused(self):
        envelope = json.dumps({"nonce": "00" * 16, "ct": "", "tag": "00" * 32}).encode()
        with self.assertRaises(MissingVersionError):
            keywrap.unwrap(self.ring, envelope)

    def test_unknown_version_refused(self):
        envelope = keywrap.wrap(self.ring, b"data")
        obj = json.loads(envelope)
        obj["v"] = 999
        with self.assertRaises(UnknownVersionError):
            keywrap.unwrap(self.ring, json.dumps(obj).encode())

    def test_tampered_ciphertext_detected(self):
        envelope = keywrap.wrap(self.ring, b"important")
        obj = json.loads(envelope)
        obj["ct"] = "00" + obj["ct"][2:]
        with self.assertRaises(IntegrityError):
            keywrap.unwrap(self.ring, json.dumps(obj).encode())

    def test_tampered_version_detected(self):
        self.ring.add_key()
        envelope = keywrap.wrap(self.ring, b"important")
        obj = json.loads(envelope)
        obj["v"] = 1  # re-label a v2 envelope as v1
        with self.assertRaises(IntegrityError):
            keywrap.unwrap(self.ring, json.dumps(obj).encode())

    def test_garbage_envelope(self):
        with self.assertRaises(EnvelopeFormatError):
            keywrap.unwrap(self.ring, b"this is not json")

    def test_aad_binding(self):
        envelope = keywrap.wrap(self.ring, b"data", aad=b"context")
        self.assertEqual(keywrap.unwrap(self.ring, envelope, aad=b"context"), b"data")
        with self.assertRaises(IntegrityError):
            keywrap.unwrap(self.ring, envelope, aad=b"other")


class TestCoexistence(KeyWrapTestCase):
    def test_old_and_new_data_decrypt_across_rotation(self):
        old_payloads = self.write_records(4, prefix="old")
        old_version = self.ring.active_version
        self.ring.add_key()
        new_payloads = self.write_records(4, prefix="new")
        self.assertNotEqual(old_version, self.ring.active_version)
        self.assert_all_records_decrypt({**old_payloads, **new_payloads})

    def test_keyring_reloaded_from_disk(self):
        envelope = keywrap.wrap(self.ring, b"persisted")
        fresh = KeyRing(self.dir / "keyring.json")
        self.assertEqual(keywrap.unwrap(fresh, envelope), b"persisted")


class TestRotation(KeyWrapTestCase):
    def test_full_rotation_and_stats(self):
        old_payloads = self.write_records(6, prefix="old")
        self.ring.add_key()
        new_payloads = self.write_records(2, prefix="new")

        progress = []
        stats = keywrap.rotate_records(
            self.record_dir, self.ring, progress_callback=lambda n, s: progress.append((n, s.scanned))
        )

        self.assertTrue(stats.done)
        self.assertEqual(stats.rotated, 6)
        self.assertEqual(stats.already_current, 2)
        self.assertEqual(stats.failed, 0)
        self.assertEqual(stats.remaining_by_version, {"2": 8})
        self.assertEqual(len(progress), 8)
        self.assertEqual([p[1] for p in progress], sorted(p[1] for p in progress))
        self.assert_all_records_decrypt({**old_payloads, **new_payloads})

    def test_interrupted_rotation_resumes(self):
        payloads = self.write_records(10)
        self.ring.add_key()

        calls = []

        def crash_after_five(name, stats):
            calls.append(name)
            if len(calls) == 5:
                raise SimulatedCrash("power loss after 5 records")

        with self.assertRaises(SimulatedCrash):
            keywrap.rotate_records(self.record_dir, self.ring, progress_callback=crash_after_five)

        # Mid-rotation: mixed versions on disk, every record still decrypts.
        self.assert_all_records_decrypt(payloads)
        remaining = keywrap.scan_versions(self.record_dir)
        self.assertEqual(sum(remaining.values()), 10)
        self.assertGreater(remaining.get("1", 0), 0)
        self.assertGreater(remaining.get("2", 0), 0)

        stats = keywrap.rotate_records(self.record_dir, self.ring)
        self.assertTrue(stats.done)
        self.assertEqual(stats.rotated + stats.already_current, 10)
        self.assertEqual(stats.remaining_by_version, {"2": 10})
        self.assert_all_records_decrypt(payloads)

    def test_crash_between_record_write_and_journal(self):
        payloads = self.write_records(6)
        self.ring.add_key()

        # Rotate one record "by hand" without touching the journal, simulating
        # a crash after the atomic record write but before the journal flush.
        first = sorted(self.record_dir.iterdir())[0]
        plaintext = keywrap.unwrap(self.ring, first.read_bytes())
        first.write_bytes(keywrap.wrap(self.ring, plaintext))

        stats = keywrap.rotate_records(self.record_dir, self.ring)
        self.assertTrue(stats.done)
        self.assertEqual(stats.rotated, 5)
        self.assertEqual(stats.already_current, 1)
        self.assert_all_records_decrypt(payloads)

    def test_corrupt_record_does_not_abort_rotation(self):
        payloads = self.write_records(4)
        self.write_record(f"broken{keywrap.RECORD_SUFFIX}", b"not an envelope")
        self.ring.add_key()

        stats = keywrap.rotate_records(self.record_dir, self.ring)
        self.assertFalse(stats.done)
        self.assertEqual(stats.rotated, 4)
        self.assertEqual(stats.failed, 1)
        self.assertEqual(stats.remaining_by_version.get("corrupt"), 1)
        self.assert_all_records_decrypt(payloads)

    def test_journal_target_conflict(self):
        self.write_records(2)
        self.ring.add_key()
        keywrap.rotate_records(self.record_dir, self.ring)
        self.ring.add_key()
        # Journal from the finished v2 campaign conflicts with a v3 campaign.
        with self.assertRaises(RotationStateError):
            keywrap.rotate_records(self.record_dir, self.ring)


class TestKeyRetirement(KeyWrapTestCase):
    def test_not_retirable_while_records_remain(self):
        self.write_records(3)
        self.ring.add_key()
        self.assertFalse(keywrap.can_retire_version(self.record_dir, self.ring, 1))
        self.assertEqual(keywrap.retire_stale_keys(self.record_dir, self.ring), [])
        self.assertTrue(self.ring.has_version(1))

    def test_retirable_after_full_rotation(self):
        payloads = self.write_records(3)
        self.ring.add_key()
        keywrap.rotate_records(self.record_dir, self.ring)
        self.assertTrue(keywrap.can_retire_version(self.record_dir, self.ring, 1))
        self.assertEqual(keywrap.retire_stale_keys(self.record_dir, self.ring), [1])
        self.assertFalse(self.ring.has_version(1))
        self.assert_all_records_decrypt(payloads)

    def test_active_key_cannot_be_retired(self):
        with self.assertRaises(KeyRetirementError):
            self.ring.retire(self.ring.active_version)

    def test_premature_key_deletion_breaks_old_data_only(self):
        old_payloads = self.write_records(2, prefix="old")
        self.ring.add_key()
        new_payloads = self.write_records(2, prefix="new")

        # Simulate an operator deleting the old key before rotation finished.
        self.ring.retire(1)

        for name in old_payloads:
            with self.assertRaises(UnknownVersionError, msg=name):
                keywrap.unwrap(self.ring, (self.record_dir / name).read_bytes())
        self.assert_all_records_decrypt(new_payloads)


class TestConcurrency(KeyWrapTestCase):
    def test_concurrent_wrap_unwrap(self):
        errors = []
        results = {}
        lock = threading.Lock()

        def worker(worker_id):
            try:
                for i in range(50):
                    plaintext = f"w{worker_id}-{i}".encode()
                    envelope = keywrap.wrap(self.ring, plaintext)
                    got = keywrap.unwrap(self.ring, envelope)
                    with lock:
                        results[(worker_id, i)] = got == plaintext
            except Exception as exc:  # collected for assertion
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertTrue(all(results.values()))
        self.assertEqual(len(results), 400)

    def test_concurrent_readers_during_rotation(self):
        payloads = self.write_records(20)
        self.ring.add_key()
        errors = []
        stop = threading.Event()

        def reader():
            names = sorted(payloads)
            i = 0
            while not stop.is_set():
                name = names[i % len(names)]
                i += 1
                try:
                    got = keywrap.unwrap(self.ring, (self.record_dir / name).read_bytes())
                    if got != payloads[name]:
                        errors.append(f"wrong plaintext for {name}")
                except Exception as exc:
                    errors.append(f"{name}: {exc!r}")

        readers = [threading.Thread(target=reader) for _ in range(4)]
        for t in readers:
            t.start()
        stats = keywrap.rotate_records(self.record_dir, self.ring)
        stop.set()
        for t in readers:
            t.join()

        self.assertTrue(stats.done)
        self.assertEqual(errors, [])
        self.assert_all_records_decrypt(payloads)

    def test_concurrent_key_additions_are_serialized(self):
        errors = []

        def adder():
            try:
                self.ring.add_key()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=adder) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(self.ring.versions(), [1, 2, 3, 4, 5, 6])


if __name__ == "__main__":
    unittest.main()
