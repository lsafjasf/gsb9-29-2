#!/usr/bin/env python3
"""Self-verifying demo: rotation timeline, rollback and crash recovery.

Run:  python3 run_demo.py
Only the standard library is used.  All time comes from an injected FakeClock.
"""
import os
import random
import tempfile

from cert_rotation import (Certificate, CertStore, FakeClock,
                           RotationOrchestrator)

T0 = 1_700_000_000.0
HOLD = 300.0        # 5 minutes between forward stages
RB_HOLD = 30.0      # 30 seconds between rollback stages


def cert(cid, valid_from=T0 - 86_400, valid_to=T0 + 30 * 86_400):
    return Certificate(cid, valid_from, valid_to, payload=f"pem:{cid}")


def header(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def check(cond, msg):
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        raise SystemExit(f"verification failed: {msg}")


def advance(clock, orch, store, seconds):
    clock.advance(seconds)
    orch.tick()
    check(not store.has_gap(), "at least one certificate is serving (no both-down window)")


def scenario_a(tmp):
    """Normal rotation before expiry: coexistence, proportional shift, retire."""
    header("Scenario A: normal rotation before certificate expiry")
    clock = FakeClock(T0)
    store = CertStore(clock, random.Random(1))
    orch = RotationOrchestrator(
        store, clock, os.path.join(tmp, "a.journal.jsonl"),
        hold_seconds=HOLD, rollback_hold_seconds=RB_HOLD)
    orch.bootstrap(cert("old"))

    established = [store.open_connection() for _ in range(3)]
    print("  initial weights:", store.weights())
    orch.start_rotation(cert("new"))
    for _ in range(5):
        advance(clock, orch, store, HOLD)
    print("  final weights:  ", store.weights())
    check(orch.mode == "done", "rotation completed")
    check(store.weights() == {"new": 100}, "new certificate serves 100% of traffic")

    print("\n  -- rotation timeline --")
    print("\n".join("    " + line for line in orch.timeline.render().splitlines()))

    check(store.connections_using("old") == 3, "3 pre-rotation connections still pinned to old cert")
    check(not store.is_retired("old"), "old cert kept (draining) while its connections live")
    for c in established:
        store.close_connection(c)
    orch.tick()
    check(store.is_retired("old"), "old cert retired only after its connections closed")


def scenario_b(tmp):
    """Failure mid-rotation -> auto rollback; manual rollback keeps conns alive."""
    header("Scenario B: rotation failure -> automatic rollback")
    clock = FakeClock(T0)
    store = CertStore(clock, random.Random(2))
    orch = RotationOrchestrator(
        store, clock, os.path.join(tmp, "b.journal.jsonl"),
        hold_seconds=HOLD, rollback_hold_seconds=RB_HOLD)
    orch.bootstrap(cert("old"))
    orch.start_rotation(cert("new"))
    advance(clock, orch, store, HOLD)  # 75/25
    advance(clock, orch, store, HOLD)  # 50/50

    pinned_new = [c for c in (store.open_connection() for _ in range(400))
                  if c.cert.cert_id == "new"]
    pinned_old = []
    for _ in range(50):
        c = store.open_connection()
        if c.cert.cert_id == "old":
            pinned_old.append(c)
        else:
            store.close_connection(c)  # probe on new cert: not needed for check
        if len(pinned_old) == 5:
            break
    check(pinned_new, "some established connections negotiated the new cert")

    # new cert starts failing its health check (e.g. OCSP/CDN edge failure)
    orch.health_check = lambda c, now: c.cert_id != "new"
    advance(clock, orch, store, HOLD)
    check(orch.mode == "rolling_back", "failure triggered automatic rollback")
    advance(clock, orch, store, RB_HOLD)
    advance(clock, orch, store, RB_HOLD)
    check(orch.mode == "done", "rollback completed")
    check(store.weights() == {"old": 100}, "old certificate serves 100% again")

    print("\n  -- rollback timeline --")
    print("\n".join("    " + line for line in orch.timeline.render().splitlines()))

    print("\n  -- connection verification after rollback --")
    check(all(c.is_usable(store) for c in pinned_new),
          f"{len(pinned_new)} established connections on NEW cert still usable (not dropped)")
    check(all(c.is_usable(store) for c in pinned_old),
          "established connections on OLD cert still usable")
    check(not store.is_retired("new"), "new cert draining, not retired while its connections live")
    new_handshakes = [store.open_connection().cert.cert_id for _ in range(100)]
    check(all(cid == "old" for cid in new_handshakes),
          "100/100 new handshakes negotiate the old cert after rollback")
    for c in pinned_new:
        store.close_connection(c)
    orch.tick()
    check(store.is_retired("new"), "new cert retired after its established connections close")


def scenario_c(tmp):
    """Crash mid-rotation and mid-rollback; restart and continue safely."""
    header("Scenario C: crash/restart during rotation and during rollback")
    clock = FakeClock(T0)
    store = CertStore(clock, random.Random(3))
    journal = os.path.join(tmp, "c.journal.jsonl")
    orch = RotationOrchestrator(
        store, clock, journal,
        hold_seconds=HOLD, rollback_hold_seconds=RB_HOLD)
    orch.bootstrap(cert("old"))
    orch.start_rotation(cert("new"))
    advance(clock, orch, store, HOLD)  # 75/25
    advance(clock, orch, store, HOLD)  # 50/50
    crash_weights = store.weights()

    print("  crash at weights:", crash_weights, "-> process restarts")
    # brand-new process: empty in-memory store, same journal/checkpoint
    store2 = CertStore(clock, random.Random(77))
    orch2 = RotationOrchestrator(
        store2, clock, journal,
        hold_seconds=HOLD, rollback_hold_seconds=RB_HOLD)
    mode = orch2.recover()
    check(mode == "rotating", "recovered into rotating state")
    check(store2.weights() == crash_weights, "weights resumed exactly at crash point")
    check(not store2.has_gap(), "no both-down window immediately after recovery")

    advance(clock, orch2, store2, HOLD)  # 25/75, then operator aborts
    orch2.rollback(reason="operator-abort")
    advance(clock, orch2, store2, RB_HOLD)  # one rollback stage, then crash
    rb_weights = store2.weights()
    print("  crash during rollback at weights:", rb_weights, "-> restarts")

    store3 = CertStore(clock, random.Random(88))
    orch3 = RotationOrchestrator(
        store3, clock, journal,
        hold_seconds=HOLD, rollback_hold_seconds=RB_HOLD)
    mode = orch3.recover()
    check(mode == "rolling_back", "recovered into rolling_back state")
    check(store3.weights() == rb_weights, "rollback resumed exactly at crash point")
    check(not store3.has_gap(), "no both-down window after recovery mid-rollback")
    advance(clock, orch3, store3, RB_HOLD)
    check(orch3.mode == "done", "rollback completed after restart")
    check(store3.weights() == {"old": 100}, "old certificate fully restored")

    print("\n  -- recovery timeline (journal replayed, then continued) --")
    print("\n".join("    " + line for line in orch3.timeline.render().splitlines()))


def scenario_d(tmp):
    header("Scenario D: rotation started AFTER the old cert already expired")
    clock = FakeClock(T0)
    store = CertStore(clock, random.Random(4))
    orch = RotationOrchestrator(
        store, clock, os.path.join(tmp, "d.journal.jsonl"),
        hold_seconds=HOLD, rollback_hold_seconds=RB_HOLD)
    orch.bootstrap(cert("old", valid_to=T0 + 120))  # expires in 2 minutes
    clock.advance(600)  # old cert now expired
    check(not orch.old_cert.is_valid_at(clock.now()), "old certificate is expired")
    orch.start_rotation(cert("new"))
    for _ in range(5):
        advance(clock, orch, store, HOLD)
    check(orch.mode == "done", "rotation after expiry completed")
    check(store.weights() == {"new": 100}, "new cert now serves all traffic")
    orch.tick()
    check(store.is_retired("old"), "expired old cert retired after drain")


def main():
    tmp = tempfile.mkdtemp(prefix="cert-rotation-demo-")
    scenario_a(tmp)
    scenario_b(tmp)
    scenario_c(tmp)
    scenario_d(tmp)
    header("ALL SCENARIOS PASSED")
    print("  journal directory:", tmp)


if __name__ == "__main__":
    main()
