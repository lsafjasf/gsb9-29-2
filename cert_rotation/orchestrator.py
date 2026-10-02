"""Rotation orchestrator.

State machine:  idle -> rotating -> done
                idle/rotating/done -> rolling_back -> done  (manual or automatic)

Every transition is journaled BEFORE it is considered effective, so a
process crash at any point can be recovered by replaying the journal
(recover()).  The store invariants guarantee that replaying any prefix of
the journal always yields a serving configuration -- never a gap.
"""
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional, Tuple

from .certs import Certificate
from .errors import RotationError, RollbackRefused
from .journal import Journal, read_checkpoint, write_checkpoint
from .timeline import Timeline


def build_stages(steps: int) -> List[Tuple[int, int]]:
    """Forward plan of (old_weight, new_weight), ending at (0, 100)."""
    stages = []
    for i in range(1, steps + 1):
        new_w = round(100 * i / steps)
        stages.append((100 - new_w, new_w))
    return stages


def build_rollback_stages(cur_new_weight: int, steps: int) -> List[Tuple[int, int]]:
    """Reverse plan from the current weights back to (100, 0)."""
    stages: List[Tuple[int, int]] = []
    for i in range(1, steps + 1):
        new_w = round(cur_new_weight * (steps - i) / steps)
        pair = (100 - new_w, new_w)
        if not stages or stages[-1] != pair:
            stages.append(pair)
    if not stages or stages[-1] != (100, 0):
        stages.append((100, 0))
    return stages


class RotationOrchestrator:
    def __init__(self, store, clock, journal_path, checkpoint_path=None,
                 health_check=None, steps: int = 4, hold_seconds: float = 60.0,
                 rollback_steps: int = 2, rollback_hold_seconds: float = 10.0,
                 timeline: Optional[Timeline] = None):
        self.store = store
        self.clock = clock
        self.journal = Journal(journal_path)
        self.checkpoint_path = (Path(checkpoint_path) if checkpoint_path
                                else Path(str(journal_path) + ".checkpoint"))
        self.health_check = health_check or (lambda cert, now: cert.is_valid_at(now))
        self.steps = steps
        self.hold_seconds = float(hold_seconds)
        self.rollback_steps = rollback_steps
        self.rollback_hold_seconds = float(rollback_hold_seconds)
        self.timeline = timeline if timeline is not None else Timeline()

        self.mode = "idle"  # idle | rotating | rolling_back | done
        self.old_cert: Optional[Certificate] = None
        self.new_cert: Optional[Certificate] = None
        self.stages: List[Tuple[int, int]] = []
        self.stage_index = -1
        self.stage_entered_at: Optional[float] = None
        self.rotation_id: Optional[str] = None
        self._drain_announced = set()

    # ------------------------------------------------------------------
    # journal / timeline
    # ------------------------------------------------------------------
    def _emit(self, event: str, **details) -> None:
        entry = {"event": event, "at": self.clock.now(),
                 "rotation_id": self.rotation_id}
        entry.update(details)
        self.journal.append(entry)
        self.timeline.record(entry["at"], event, **details)

    def _write_checkpoint(self) -> None:
        certs = [asdict(c) for c in (self.old_cert, self.new_cert) if c]
        write_checkpoint(self.checkpoint_path,
                         {"weights": self.store.weights(), "certs": certs})

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def bootstrap(self, old_cert: Certificate) -> None:
        if self.store.weights():
            raise RotationError("store already initialised")
        self.old_cert = old_cert
        self.store.add_cert(old_cert)
        self.store.bootstrap(old_cert)
        self.rotation_id = "rot-0"
        self._emit("bootstrapped", old=old_cert.cert_id, old_cert=asdict(old_cert))
        self._write_checkpoint()

    def start_rotation(self, new_cert: Certificate) -> None:
        if self.mode not in ("idle", "done"):
            raise RotationError(f"cannot start rotation while {self.mode}")
        if self.old_cert is None:
            raise RotationError("orchestrator not bootstrapped")
        now = self.clock.now()
        if not new_cert.is_valid_at(now):
            raise RotationError("new certificate is not valid at current time")
        # Note: the OLD cert may already be expired -- rotation after expiry
        # is a supported case; only the new cert must be valid.
        self.store.add_cert(new_cert)
        self.new_cert = new_cert
        self.stages = build_stages(self.steps)
        self.stage_index = -1
        self.stage_entered_at = now
        self.rotation_id = f"rot-{int(now * 1000)}"
        self.mode = "rotating"
        self._emit("rotation_started",
                   old=self.old_cert.cert_id, new=new_cert.cert_id,
                   old_cert=asdict(self.old_cert), new_cert=asdict(new_cert),
                   stages=[list(s) for s in self.stages],
                   hold_seconds=self.hold_seconds)

    def tick(self) -> None:
        """Advance the state machine according to the injected clock."""
        now = self.clock.now()
        if self.mode == "rotating":
            self._tick_rotating(now)
        elif self.mode == "rolling_back":
            self._tick_rollback(now)
        self._maybe_retire()

    def rollback(self, reason: str = "manual", force: bool = False) -> None:
        """Operator-initiated rollback. Refused if the old cert is already
        expired, unless force=True (auto-rollback after a failed rotation is
        always forced: restoring the previous known state is safer than
        keeping a failing new cert)."""
        if self.mode not in ("rotating", "done"):
            raise RotationError(f"cannot rollback while {self.mode}")
        if self.old_cert is None or self.store.is_retired(self.old_cert.cert_id):
            raise RotationError("old certificate already retired; cannot rollback")
        now = self.clock.now()
        if not force and not self.old_cert.is_valid_at(now):
            self._emit("rollback_refused", reason="old_cert_expired",
                       old=self.old_cert.cert_id)
            raise RollbackRefused(
                "old certificate is expired; pass force=True to override")
        self._begin_rollback(now, reason=reason, automatic=False)

    def recover(self) -> str:
        """Rebuild state after a restart. Never leaves the store without a
        serving certificate: replay the journal; if that fails or is empty,
        fall back to the atomic checkpoint."""
        entries, truncated = self.journal.read_all()
        if truncated:
            self.timeline.record(self.clock.now(), "journal_truncated",
                                 note="ignored corrupt tail; resuming from last valid entry")
        try:
            self._replay(entries)
        except Exception as exc:  # defensive: journal inconsistent
            self.timeline.record(self.clock.now(), "journal_replay_failed",
                                 error=str(exc))
            entries = []
        if not entries or self.store.has_gap():
            checkpoint = read_checkpoint(self.checkpoint_path)
            if checkpoint:
                for c in checkpoint.get("certs", []):
                    cert = Certificate(**c)
                    if not self.store.is_retired(cert.cert_id):
                        self.store.add_cert(cert)
                self.store.apply_weights(checkpoint["weights"])
                self.timeline.record(self.clock.now(), "checkpoint_restored",
                                     weights=str(checkpoint["weights"]))
        if self.mode in ("rotating", "rolling_back"):
            self.timeline.record(self.clock.now(), "recovered",
                                 mode=self.mode, stage_index=self.stage_index)
        return self.mode

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _healthy(self, cert: Certificate, now: float) -> bool:
        try:
            return bool(self.health_check(cert, now))
        except Exception:
            return False

    def _apply_stage(self) -> None:
        old_w, new_w = self.stages[self.stage_index]
        weights = {}
        if old_w > 0:
            weights[self.old_cert.cert_id] = old_w
        if new_w > 0:
            weights[self.new_cert.cert_id] = new_w
        self.store.apply_weights(weights)
        self._emit("stage_applied", stage_index=self.stage_index,
                   old_weight=old_w, new_weight=new_w, weights=weights)
        self._write_checkpoint()

    def _tick_rotating(self, now: float) -> None:
        if now - self.stage_entered_at < self.hold_seconds:
            return
        if self.stage_index + 1 < len(self.stages):
            if not self._healthy(self.new_cert, now):
                self._emit("health_check_failed", cert=self.new_cert.cert_id,
                           next_stage=self.stage_index + 1)
                self._begin_rollback(now, reason="health_check_failed",
                                     automatic=True)
                return
            self.stage_index += 1
            self.stage_entered_at = now
            self._apply_stage()
        else:
            # final stage (0/100) has been held; confirm health, then complete
            if not self._healthy(self.new_cert, now):
                self._emit("health_check_failed", cert=self.new_cert.cert_id,
                           next_stage=-1)
                self._begin_rollback(now, reason="health_check_failed",
                                     automatic=True)
                return
            self.mode = "done"
            self._emit("rotation_completed", new=self.new_cert.cert_id)

    def _begin_rollback(self, now: float, reason: str, automatic: bool) -> None:
        cur_new_w = self.store.weights().get(self.new_cert.cert_id, 0)
        self.stages = build_rollback_stages(cur_new_w, self.rollback_steps)
        self.stage_index = -1
        self.stage_entered_at = now
        self.mode = "rolling_back"
        self._emit("rollback_started", reason=reason, automatic=automatic,
                   stages=[list(s) for s in self.stages])

    def _tick_rollback(self, now: float) -> None:
        if now - self.stage_entered_at < self.rollback_hold_seconds:
            return
        if self.stage_index + 1 < len(self.stages):
            self.stage_index += 1
            self.stage_entered_at = now
            self._apply_stage()
            if self.stage_index == len(self.stages) - 1:  # reached (100, 0)
                self.mode = "done"
                self._emit("rollback_completed", old=self.old_cert.cert_id)

    def _maybe_retire(self) -> None:
        if self.mode != "done":
            return
        for cert in (self.old_cert, self.new_cert):
            if cert is None:
                continue
            cid = cert.cert_id
            if self.store.is_retired(cid):
                continue
            if self.store.weights().get(cid, 0) > 0:
                continue
            n = self.store.connections_using(cid)
            if n == 0:
                if self.store.retire(cid):
                    self._emit("cert_retired", cert_id=cid)
            elif cid not in self._drain_announced:
                self._drain_announced.add(cid)
                self._emit("cert_draining", cert_id=cid, open_connections=n)

    def _replay(self, entries) -> None:
        for e in entries:
            ev = e["event"]
            at = e["at"]
            if ev == "bootstrapped":
                self.old_cert = Certificate(**e["old_cert"])
                self.store.add_cert(self.old_cert)
                self.store.bootstrap(self.old_cert)
                self.rotation_id = e.get("rotation_id")
            elif ev == "rotation_started":
                self.old_cert = Certificate(**e["old_cert"])
                self.new_cert = Certificate(**e["new_cert"])
                self.store.add_cert(self.old_cert)
                self.store.add_cert(self.new_cert)
                self.stages = [tuple(s) for s in e["stages"]]
                self.rotation_id = e.get("rotation_id")
                self.mode = "rotating"
                self.stage_index = -1
                self.stage_entered_at = at
            elif ev == "stage_applied":
                self.stage_index = e["stage_index"]
                self.stage_entered_at = at
                self.store.apply_weights(e["weights"])
            elif ev == "rotation_completed":
                self.mode = "done"
            elif ev == "rollback_started":
                self.stages = [tuple(s) for s in e["stages"]]
                self.mode = "rolling_back"
                self.stage_index = -1
                self.stage_entered_at = at
            elif ev == "rollback_completed":
                self.mode = "done"
            elif ev == "cert_retired":
                self.store.retire(e["cert_id"])
            details = {k: v for k, v in e.items() if k not in ("event", "at")}
            self.timeline.record(at, ev, **details)
