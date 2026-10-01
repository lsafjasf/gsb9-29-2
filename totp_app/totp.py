"""TOTP (RFC 6238) generation and verification.

Standard library only. Time and storage are injectable:

* ``time_func`` -- any zero-argument callable returning a UNIX timestamp.
* ``ReplayStore`` -- replay-protection storage; in-memory + JSON file backed,
  so state survives process restarts.

Features:
  * RFC 4226 HOTP core / RFC 6238 TOTP (SHA-1, SHA-256, SHA-512, 6-10 digits).
  * Multi-window tolerance with explicit boundary semantics.
  * Per-user adaptive clock-drift estimation (EMA + variance).
  * Constant-time comparison (``hmac.compare_digest``), no early exit.
  * Replay protection with bounded memory and crash-safe persistence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import struct
import threading
import time as _time
from collections import OrderedDict
from typing import Callable, Dict, Optional, Tuple

# RFC 4226 section 4 ("R6") and RFC 6238 section 5 require >= 128 bit secrets.
MIN_SECRET_BYTES = 16

DIGESTS = {
    "sha1": hashlib.sha1,
    "sha256": hashlib.sha256,
    "sha512": hashlib.sha512,
}


# ---------------------------------------------------------------------------
# Key / parameter validation
# ---------------------------------------------------------------------------

def _validate_secret(secret: bytes) -> bytes:
    if not isinstance(secret, (bytes, bytearray)):
        raise TypeError("secret must be bytes (use base32decode for user keys)")
    if len(secret) < MIN_SECRET_BYTES:
        raise ValueError(
            "secret too short: %d bytes; RFC 4226/6238 require >= %d bytes (128 bit)"
            % (len(secret), MIN_SECRET_BYTES)
        )
    return bytes(secret)


def _validate_digits(digits: int) -> int:
    if not isinstance(digits, int) or isinstance(digits, bool):
        raise TypeError("digits must be an int")
    if not 6 <= digits <= 10:
        # HOTP truncation yields 31 bits, so at most 10 decimal digits.
        raise ValueError("digits out of supported range 6..10: %r" % (digits,))
    return digits


def _validate_step(step: int) -> int:
    if not isinstance(step, int) or isinstance(step, bool) or step <= 0:
        raise ValueError("step must be a positive int (seconds)")
    return step


def _validate_digest(digest: str) -> str:
    name = digest.lower().replace("-", "")
    if name not in DIGESTS:
        raise ValueError("unsupported digest %r (use sha1/sha256/sha512)" % (digest,))
    return name


# ---------------------------------------------------------------------------
# HOTP / TOTP core
# ---------------------------------------------------------------------------

def hotp(secret: bytes, counter: int, digits: int = 6, digest: str = "sha1") -> str:
    """RFC 4226 HOTP: HMAC-based one-time password for a given counter."""
    secret = _validate_secret(secret)
    digits = _validate_digits(digits)
    digest = _validate_digest(digest)
    if not isinstance(counter, int) or isinstance(counter, bool) or counter < 0:
        raise ValueError("counter must be a non-negative int")

    msg = struct.pack(">Q", counter)
    mac = hmac.new(secret, msg, DIGESTS[digest]).digest()

    # Dynamic truncation (RFC 4226 section 5.3).
    offset = mac[-1] & 0x0F
    code_int = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code_int % (10 ** digits)).zfill(digits)


def time_counter(for_time: float, step: int = 30) -> int:
    """Map a timestamp to a TOTP counter.

    Boundary semantics (see README "Window boundaries"):
        counter(t) = floor(t / step)
    so window ``k`` is the half-open interval ``[k*step, (k+1)*step)``.
    At the exact instant t == k*step the code for window *k* is in effect.
    """
    if for_time < 0:
        raise ValueError("for_time must be >= 0")
    return math.floor(for_time / step)


def totp(
    secret: bytes,
    for_time: Optional[float] = None,
    step: int = 30,
    digits: int = 6,
    digest: str = "sha1",
    time_func: Callable[[], float] = _time.time,
) -> str:
    """Generate a TOTP for the supplied (or injected) time."""
    step = _validate_step(step)
    if for_time is None:
        for_time = time_func()
    return hotp(secret, time_counter(for_time, step), digits, digest)


def constant_time_equals(a: str, b: str) -> bool:
    """Constant-time equality for password strings.

    Wraps :func:`hmac.compare_digest`, which performs a byte-wise comparison
    whose running time does not depend on *where* the strings differ -- only
    on their length. Length difference is not secret-derived here (the length
    is the configured digit count), so it carries no useful guessing signal.
    """
    if not isinstance(a, str) or not isinstance(b, str):
        raise TypeError("constant_time_equals expects str")
    return hmac.compare_digest(a, b)


# ---------------------------------------------------------------------------
# Adaptive drift estimation
# ---------------------------------------------------------------------------

class DriftEstimator:
    """Per-user clock-drift model.

    Each successful verification yields an *offset* d, i.e. how many windows
    away from the server's current window the accepted code lived:

        d = matched_counter - server_counter

    Positive d means the client clock runs ahead.

    The model keeps an exponentially weighted mean and variance of d
    (Westfeld-style EWMA variance), and derives the accepted window range:

        center = round(ema)
        spread = base + ceil(2 * std)           (clamped to max_extra)
        accepted counters = [center - spread, center + spread]

    On first sight of a user a wide ``bootstrap`` window is used so a device
    with substantial skew can enrol; the window tightens as stable evidence
    accumulates. Wild offsets (e.g. caused by a server time jump) are clamped
    before they can poison the estimate.
    """

    def __init__(
        self,
        base_spread: int = 1,
        bootstrap_spread: int = 4,
        bootstrap_samples: int = 5,
        max_extra: int = 10,
        alpha: float = 0.3,
        max_learn_offset: int = 10,
    ) -> None:
        self.base_spread = base_spread
        self.bootstrap_spread = bootstrap_spread
        self.bootstrap_samples = bootstrap_samples
        self.max_extra = max_extra
        self.alpha = alpha
        self.max_learn_offset = max_learn_offset
        self.samples = 0
        self.ema = 0.0
        self.var = 0.0

    def observe(self, offset: int) -> None:
        """Feed one verified offset (clamped against time-jump outliers)."""
        offset = max(-self.max_learn_offset, min(self.max_learn_offset, offset))
        if self.samples == 0:
            self.ema = float(offset)
            self.var = 0.0
        else:
            delta = offset - self.ema
            self.ema += self.alpha * delta
            self.var = (1 - self.alpha) * (self.var + self.alpha * delta * delta)
        self.samples += 1

    @property
    def std(self) -> float:
        return math.sqrt(self.var)

    def window(self) -> Tuple[int, int]:
        """Return (low_offset, high_offset) relative to the server counter."""
        if self.samples < self.bootstrap_samples:
            spread = self.bootstrap_spread
        else:
            spread = self.base_spread + int(math.ceil(2.0 * self.std))
            spread = min(spread, self.bootstrap_spread + self.max_extra)
        center = int(round(self.ema))
        return center - spread, center + spread

    def to_dict(self) -> dict:
        return {
            "samples": self.samples,
            "ema": self.ema,
            "var": self.var,
        }

    @classmethod
    def from_dict(cls, data: dict, **kwargs) -> "DriftEstimator":
        est = cls(**kwargs)
        est.samples = int(data.get("samples", 0))
        est.ema = float(data.get("ema", 0.0))
        est.var = float(data.get("var", 0.0))
        return est


# ---------------------------------------------------------------------------
# Bounded, persistent replay store
# ---------------------------------------------------------------------------

class ReplayStore:
    """Remembers used (user, counter) pairs so a code cannot be reused.

    * Memory bound: at most ``max_entries`` records are held; eviction drops
      expired records first, then the record with the earliest expiry, so the
      store never grows past its cap.
    * Persistence: records are dumped atomically (tmp file + os.replace) to a
      JSON file; :meth:`load` restores them after a restart, so codes already
      consumed remain rejected across restarts.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        max_entries: int = 10_000,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self.path = path
        self.max_entries = max_entries
        self._entries: "OrderedDict[str, int]" = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _key(user: str, counter: int) -> str:
        return "%s\x00%d" % (user, counter)

    def __len__(self) -> int:
        return len(self._entries)

    def check_and_record(self, user: str, counter: int, expire_counter: int,
                         now_counter: Optional[int] = None) -> bool:
        """Atomically consume (user, counter).

        Returns True if the code is fresh (and has now been recorded),
        False if it was already consumed. Records whose expiry counter has
        been reached (``expire <= now_counter``) are purged first.
        """
        if now_counter is None:
            now_counter = counter
        key = self._key(user, counter)
        with self._lock:
            if key in self._entries:
                return False
            self._entries[key] = expire_counter
            self._evict(now_counter)
            self._persist_locked()
            return True

    def _evict(self, now_counter: int) -> None:
        # 1. Drop anything already expired.
        expired = [k for k, exp in self._entries.items() if exp <= now_counter]
        for k in expired:
            del self._entries[k]
        # 2. If still over capacity, drop earliest-expiry first (LRU-ish by TTL).
        while len(self._entries) > self.max_entries:
            victim = min(self._entries, key=lambda k: self._entries[k])
            del self._entries[victim]

    def _persist_locked(self) -> None:
        if self.path is None:
            return
        payload = [[k, v] for k, v in self._entries.items()]
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def save(self) -> None:
        with self._lock:
            self._persist_locked()

    @classmethod
    def load(cls, path: str, max_entries: int = 10_000) -> "ReplayStore":
        store = cls(path=path, max_entries=max_entries)
        if not os.path.exists(path):
            return store
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        for key, exp in payload:
            store._entries[key] = int(exp)
        return store


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------

class VerificationResult:
    def __init__(self, ok: bool, matched_offset: Optional[int] = None,
                 reason: str = "") -> None:
        self.ok = ok
        self.matched_offset = matched_offset
        self.reason = reason

    def __bool__(self) -> bool:
        return self.ok


class Verifier:
    """TOTP verifier combining drift tolerance and replay protection.

    Parameters
    ----------
    store:
        ReplayStore (shared or per-instance).
    step, digits, digest:
        TOTP parameters agreed with the client.
    time_func:
        Injectable clock (defaults to wall clock).
    replay_windows:
        A consumed code is forgotten ``replay_windows`` windows after the end
        of its own window. Must be >= max accepted |offset| in practice.
    """

    def __init__(
        self,
        store: Optional[ReplayStore] = None,
        step: int = 30,
        digits: int = 6,
        digest: str = "sha1",
        time_func: Callable[[], float] = _time.time,
        replay_windows: int = 10,
        drift_kwargs: Optional[dict] = None,
    ) -> None:
        self.store = store if store is not None else ReplayStore()
        self.step = _validate_step(step)
        self.digits = _validate_digits(digits)
        self.digest = _validate_digest(digest)
        self.time_func = time_func
        self.replay_windows = replay_windows
        self._drift: Dict[str, DriftEstimator] = {}
        self._drift_factory = lambda: DriftEstimator(**(drift_kwargs or {}))
        self._lock = threading.Lock()

    def _estimator(self, user: str) -> DriftEstimator:
        est = self._drift.get(user)
        if est is None:
            est = self._drift_factory()
            self._drift[user] = est
        return est

    def verify(self, user: str, secret: bytes, otp: str) -> VerificationResult:
        """Verify one submitted code.

        Timing notes:
          * Inputs of the wrong length/shape are rejected before any crypto;
            length is public configuration, not secret.
          * Every counter in the accepted range is computed and compared with
            :func:`hmac.compare_digest`; the loop does NOT break at the first
            match, so response time reveals neither the matching window nor a
            matching prefix.
          * Replay bookkeeping happens only after the full scan.
        """
        secret = _validate_secret(secret)
        if not isinstance(otp, str) or len(otp) != self.digits or not otp.isdigit():
            return VerificationResult(False, reason="bad_format")

        now = self.time_func()
        server_counter = time_counter(now, self.step)

        with self._lock:
            est = self._estimator(user)
            low_off, high_off = est.window()

        matched: Optional[int] = None
        # Full, branch-free scan: compare every candidate, record at most the
        # lowest matching offset, but never short-circuit the loop.
        for offset in range(low_off, high_off + 1):
            candidate_counter = server_counter + offset
            if candidate_counter < 0:
                continue  # clock jumped back before epoch; skip, keep timing
            candidate = hotp(secret, candidate_counter,
                             self.digits, self.digest)
            if constant_time_equals(candidate, otp):
                if matched is None:
                    matched = offset

        if matched is None:
            return VerificationResult(False, reason="no_match")

        matched_counter = server_counter + matched
        expire_counter = matched_counter + self.replay_windows
        if not self.store.check_and_record(user, matched_counter,
                                           expire_counter,
                                           now_counter=matched_counter):
            return VerificationResult(False, matched_offset=matched,
                                      reason="replayed")

        with self._lock:
            est.observe(matched)
        return VerificationResult(True, matched_offset=matched)
