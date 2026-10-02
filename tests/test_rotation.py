"""Self-tests for certificate rotation orchestration.

Run:  python3 -m unittest discover -s tests -v
"""
import os
import random
import tempfile
import unittest

from cert_rotation import (Certificate, CertStore, FakeClock, RotationError,
                           RollbackRefused, RotationOrchestrator, Timeline)

T0 = 1_700_000_000.0
HOLD = 100.0
RB_HOLD = 10.0


def make_cert(cid, valid_from=T0 - 1000, valid_to=T0 + 10_000):
    return Certificate(cid, valid_from, valid_to, payload=f"pem-body:{cid}")


class RotationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rot-")
        self.clock = FakeClock(T0)
        self.store = CertStore(self.clock, random.Random(42))
        self.orch = RotationOrchestrator(
            self.store, self.clock,
            os.path.join(self.tmp, "journal.jsonl"),
            hold_seconds=HOLD, rollback_hold_seconds=RB_HOLD)
        self.old = make_cert("old")
        self.new = make_cert("new")
        self.orch.bootstrap(self.old)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def start(self, **kw):
        new = kw.pop("new", self.new)
        orch = kw.pop("orch", self.orch)
        orch.start_rotation(new)

    def step(self, seconds=None):
        self.clock.advance(HOLD if seconds is None else seconds)
        self.orch.tick()
        self.assert_no_gap()

    def rb_step(self):
        self.clock.advance(RB_HOLD)
        self.orch.tick()
        self.assert_no_gap()

    def run_to_done(self):
        for _ in range(8):
            self.step()
            if self.orch.mode == "done":
                return
        self.fail("rotation never completed")

    def assert_no_gap(self):
        w = self.store.weights()
        self.assertTrue(w, "NO-CERT GAP: store has no active certificate")
        self.assertEqual(sum(w.values()), 100, f"weights {w} do not sum to 100")
        self.assertFalse(self.store.has_gap())

    def events(self):
        return self.orch.timeline.names()

    # ------------------------------------------------------------------
    # 1. normal rotation before expiry: proportional shift + timeline
    # ------------------------------------------------------------------
    def test_normal_rotation_before_expiry(self):
        established = [self.store.open_connection() for _ in range(5)]
        self.start()

        expected = [{"old": 75, "new": 25}, {"old": 50, "new": 50},
                    {"old": 25, "new": 75}, {"new": 100}]
        for exp in expected:
            self.step()
            self.assertEqual(self.store.weights(), exp)

        # final stage must be held + health-confirmed before completion
        self.assertNotEqual(self.orch.mode, "done")
        self.step()
        self.assertEqual(self.orch.mode, "done")
        self.assertEqual(self.store.weights(), {"new": 100})

        names = self.events()
        self.assertIn("rotation_started", names)
        self.assertEqual(names.count("stage_applied"), 4)
        self.assertIn("rotation_completed", names)

        # old cert drains while established connections are still open ...
        self.orch.tick()
        self.assertFalse(self.store.is_retired("old"))
        self.assertIn("cert_draining", names)
        self.assertEqual(self.store.connections_using("old"), 5)
        # ... and is retired once they close
        for c in established:
            self.store.close_connection(c)
        self.orch.tick()
        self.assertTrue(self.store.is_retired("old"))
        self.assertIn("cert_retired", self.events())

    # 2. gradual shift actually steers new handshakes
    # ------------------------------------------------------------------
    def test_gradual_traffic_split(self):
        self.start()
        self.step(); self.step()  # 50/50
        self.assertEqual(self.store.weights(), {"old": 50, "new": 50})
        counts = {"old": 0, "new": 0}
        for _ in range(400):
            counts[self.store.open_connection().cert.cert_id] += 1
        self.assertGreater(counts["new"], 140)
        self.assertGreater(counts["old"], 140)
        self.assertLess(counts["new"], 260)

        self.step(); self.step(); self.step()
        self.assertEqual(self.store.weights(), {"new": 100})
        self.assertTrue(all(self.store.open_connection().cert.cert_id == "new"
                            for _ in range(50)))

    # ------------------------------------------------------------------
    # 3. rotation AFTER the old certificate has already expired
    # ------------------------------------------------------------------
    def test_rotation_after_expiry(self):
        expired_old = make_cert("old-expired", valid_to=T0 + 150)
        store2 = CertStore(self.clock, random.Random(1))
        orch2 = RotationOrchestrator(
            store2, self.clock,
            os.path.join(self.tmp, "j2.jsonl"),
            hold_seconds=HOLD, rollback_hold_seconds=RB_HOLD)
        orch2.bootstrap(expired_old)
        orch2.start_rotation(self.new)
        for _ in range(6):
            self.clock.advance(HOLD)
            orch2.tick()
            self.assertFalse(store2.has_gap())
        self.assertEqual(orch2.mode, "done")
        self.assertEqual(store2.weights(), {"new": 100})
        self.assertGreater(self.clock.now(), expired_old.not_after)
        self.assertIn("rotation_completed", orch2.timeline.names())

    def test_cannot_rotate_into_invalid_cert(self):
        bad = make_cert("new-bad", valid_to=T0 - 1)
        with self.assertRaises(RotationError):
            self.orch.start_rotation(bad)

    # ------------------------------------------------------------------
    # 4. rotation failure -> automatic rollback
    # ------------------------------------------------------------------
    def test_rotation_failure_triggers_automatic_rollback(self):
        fail = {"on": False}
        self.orch.health_check = (
            lambda cert, now: not (fail["on"] and cert.cert_id == "new"))
        self.start()
        self.step()  # 75/25 applied fine
        self.assertEqual(self.store.weights(), {"old": 75, "new": 25})

        fail["on"] = True  # new cert starts failing health checks
        self.step()
        self.assertEqual(self.orch.mode, "rolling_back")
        names = self.events()
        self.assertIn("health_check_failed", names)
        rb = [e for e in self.orch.timeline.events
              if e.event == "rollback_started"][-1]
        self.assertTrue(rb.details["automatic"])
        self.assertEqual(rb.details["reason"], "health_check_failed")

        self.rb_step(); self.rb_step()
        self.assertEqual(self.orch.mode, "done")
        self.assertEqual(self.store.weights(), {"old": 100})
        self.assertIn("rollback_completed", self.events())

    def test_rollback_refused_after_old_expired_unless_forced(self):
        short_old = make_cert("old-short", valid_to=T0 + 150)
        store3 = CertStore(self.clock, random.Random(7))
        orch3 = RotationOrchestrator(
            store3, self.clock, os.path.join(self.tmp, "j3.jsonl"),
            hold_seconds=HOLD, rollback_hold_seconds=RB_HOLD)
        orch3.bootstrap(short_old)
        orch3.start_rotation(self.new)
        self.clock.advance(HOLD)
        orch3.tick()  # one stage applied
        self.clock.advance(100)  # old cert now expired
        before = store3.weights()
        with self.assertRaises(RollbackRefused):
            orch3.rollback()
        self.assertEqual(orch3.mode, "rotating")  # mode untouched
        self.assertEqual(store3.weights(), before)
        self.assertIn("rollback_refused", orch3.timeline.names())

        orch3.rollback(force=True)
        self.clock.advance(RB_HOLD); orch3.tick()
        self.clock.advance(RB_HOLD); orch3.tick()
        self.assertEqual(orch3.mode, "done")
        self.assertEqual(store3.weights(), {"old-short": 100})

    # ------------------------------------------------------------------
    # 5. manual rollback does not disturb established connections
    # ------------------------------------------------------------------
    def test_manual_rollback_preserves_established_connections(self):
        old_conns = [self.store.open_connection() for _ in range(5)]
        self.start()
        self.step(); self.step()  # 50/50
        new_conns = []
        for _ in range(200):
            c = self.store.open_connection()
            if c.cert.cert_id == "new":
                new_conns.append(c)
        self.assertTrue(new_conns, "expected some connections on the new cert")

        self.orch.rollback(reason="operator-abort")
        self.rb_step(); self.rb_step()
        self.assertEqual(self.orch.mode, "done")
        self.assertEqual(self.store.weights(), {"old": 100})

        # established connections on the NEW cert keep working untouched
        self.assertTrue(all(c.is_usable(self.store) for c in new_conns))
        self.assertTrue(all(c.cert.cert_id == "new" for c in new_conns))
        self.assertEqual(self.store.connections_using("new"), len(new_conns))
        # connections on the OLD cert are unaffected too
        self.assertTrue(all(c.is_usable(self.store) for c in old_conns))
        # new cert is draining (weight 0), must NOT be retired yet
        self.assertFalse(self.store.is_retired("new"))
        self.orch.tick()
        self.assertIn("cert_draining", self.events())

        # new handshakes all get the old cert again
        self.assertTrue(all(self.store.open_connection().cert.cert_id == "old"
                            for _ in range(20)))

        # once the established new-cert connections close, new cert retires
        for c in new_conns:
            self.store.close_connection(c)
        self.orch.tick()
        self.assertTrue(self.store.is_retired("new"))

    # ------------------------------------------------------------------
    # 6. crash during ROTATION -> resume from journal
    # ------------------------------------------------------------------
    def test_interrupted_rotation_resumes(self):
        self.start()
        self.step()  # 75/25 journaled + checkpointed
        crash_weights = self.store.weights()

        # simulate process restart: brand new store + orchestrator, same files
        store_b = CertStore(self.clock, random.Random(99))
        orch_b = RotationOrchestrator(
            store_b, self.clock, self.orch.journal.path,
            hold_seconds=HOLD, rollback_hold_seconds=RB_HOLD)
        mode = orch_b.recover()

        self.assertEqual(mode, "rotating")
        self.assertEqual(orch_b.stage_index, 0)
        self.assertEqual(store_b.weights(), crash_weights)
        self.assertFalse(store_b.has_gap())
        self.assertIn("recovered", orch_b.timeline.names())

        # drive the recovered orchestrator to the end
        for _ in range(6):
            self.clock.advance(HOLD)
            orch_b.tick()
            self.assertFalse(store_b.has_gap())
        self.assertEqual(orch_b.mode, "done")
        self.assertEqual(store_b.weights(), {"new": 100})
        # exactly 4 stage transitions in the whole combined timeline
        self.assertEqual(orch_b.timeline.names().count("stage_applied"), 4)

    # ------------------------------------------------------------------
    # 7. crash during ROLLBACK -> rollback completes after restart
    # ------------------------------------------------------------------
    def test_interrupted_rollback_resumes(self):
        self.start()
        self.step(); self.step()  # 50/50
        pinned = [c for c in (self.store.open_connection() for _ in range(200))
                  if c.cert.cert_id == "new"]
        self.assertTrue(pinned)
        self.orch.rollback(reason="operator-abort")
        self.rb_step()  # first rollback stage applied, then crash
        self.assertEqual(self.orch.mode, "rolling_back")
        crash_weights = self.store.weights()

        store_b = CertStore(self.clock, random.Random(123))
        orch_b = RotationOrchestrator(
            store_b, self.clock, self.orch.journal.path,
            hold_seconds=HOLD, rollback_hold_seconds=RB_HOLD)
        mode = orch_b.recover()
        self.assertEqual(mode, "rolling_back")
        self.assertEqual(store_b.weights(), crash_weights)
        self.assertFalse(store_b.has_gap())

        self.clock.advance(RB_HOLD)
        orch_b.tick()
        self.assertEqual(orch_b.mode, "done")
        self.assertEqual(store_b.weights(), {"old": 100})

    # ------------------------------------------------------------------
    # 8. corrupt journal -> safe fallback, never a both-down window
    # ------------------------------------------------------------------
    def test_torn_journal_tail_is_ignored(self):
        self.start()
        self.step()
        with open(self.orch.journal.path, "a", encoding="utf-8") as f:
            f.write('{"event": "stage_applied", "at": 123, BROKEN')  # torn write
        store_b = CertStore(self.clock, random.Random(5))
        orch_b = RotationOrchestrator(
            store_b, self.clock, self.orch.journal.path,
            hold_seconds=HOLD, rollback_hold_seconds=RB_HOLD)
        mode = orch_b.recover()
        self.assertEqual(mode, "rotating")
        self.assertIn("journal_truncated", orch_b.timeline.names())
        self.assertEqual(store_b.weights(), {"old": 75, "new": 25})

    def test_total_journal_loss_restores_checkpoint(self):
        self.start()
        self.step(); self.step()  # 50/50 checkpointed
        with open(self.orch.journal.path, "w", encoding="utf-8") as f:
            f.write("GARBAGE GARBAGE\n")
        store_b = CertStore(self.clock, random.Random(5))
        orch_b = RotationOrchestrator(
            store_b, self.clock, self.orch.journal.path,
            hold_seconds=HOLD, rollback_hold_seconds=RB_HOLD)
        mode = orch_b.recover()
        self.assertEqual(mode, "idle")
        self.assertEqual(store_b.weights(), {"old": 50, "new": 50})
        self.assertFalse(store_b.has_gap())
        self.assertIn("checkpoint_restored", orch_b.timeline.names())

    # ------------------------------------------------------------------
    # 9. store-level invariants
    # ------------------------------------------------------------------
    def test_store_refuses_gap_and_bad_weights(self):
        with self.assertRaises(RotationError):
            self.store.apply_weights({})
        with self.assertRaises(RotationError):
            self.store.apply_weights({"old": 0, "new": 0})
        with self.assertRaises(RotationError):
            self.store.apply_weights({"old": 60, "new": 30})
        with self.assertRaises(RotationError):
            self.store.apply_weights({"old": 50, "ghost": 50})
        with self.assertRaises(RotationError):
            CertStore(self.clock).open_connection()

    def test_retire_blocked_while_weight_or_connections_exist(self):
        self.assertFalse(self.store.retire("old"))  # still weight 100
        self.start()
        self.step(); self.step()  # 50/50
        pinned = None
        for _ in range(200):
            cand = self.store.open_connection()
            if cand.cert.cert_id == "old":
                pinned = cand
                break
        self.assertIsNotNone(pinned)
        self.step(); self.step(); self.step()  # reach 100% new, done
        self.assertEqual(self.orch.mode, "done")
        # 0-weight old cert with a live connection cannot be retired
        self.orch.tick()
        self.assertFalse(self.store.is_retired("old"))
        self.store.close_connection(pinned)
        self.orch.tick()
        self.assertTrue(self.store.is_retired("old"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
