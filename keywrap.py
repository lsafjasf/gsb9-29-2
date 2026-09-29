"""Versioned key wrapping with interruptible, resumable rotation.

Stdlib-only envelope encryption:
- Each keyring version holds a random 256-bit master key.
- Per-wrap keys are derived with HKDF-HMAC-SHA256 (enc key + mac key).
- Encryption is a HMAC-DRBG keystream XOR, authenticated encrypt-then-MAC
  with HMAC-SHA256 over (prefix || version || nonce || ciphertext || aad).

Every envelope carries its key version; decryption selects the key strictly
by that version and refuses to guess when the version is missing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

try:
    import fcntl
except ImportError:  # non-POSIX fallback: thread lock only
    fcntl = None

_KEY_SIZE = 32
_NONCE_SIZE = 16
_TAG_PREFIX = b"keywrap-v1"
_HKDF_SALT = b"keywrap-hkdf-salt"
RECORD_SUFFIX = ".env"
JOURNAL_NAME = ".rotation_journal.json"


class KeyWrapError(Exception):
    """Base class for all keywrap errors."""


class EnvelopeFormatError(KeyWrapError):
    """Envelope is not a well-formed wrapped payload."""


class MissingVersionError(EnvelopeFormatError):
    """Envelope carries no key version; guessing is refused."""


class UnknownVersionError(KeyWrapError):
    """Envelope references a key version not present in the keyring."""


class IntegrityError(KeyWrapError):
    """Authentication tag mismatch: data was tampered with or key is wrong."""


class RotationStateError(KeyWrapError):
    """Rotation journal conflicts with the requested rotation."""


class KeyRetirementError(KeyWrapError):
    """Refused to retire a key that is still required."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hkdf(master: bytes, info: bytes) -> bytes:
    prk = hmac.new(_HKDF_SALT, master, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()


def _derive(master: bytes) -> tuple:
    return _hkdf(master, b"enc"), _hkdf(master, b"mac")


def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(
            enc_key, nonce + counter.to_bytes(8, "big"), hashlib.sha256
        ).digest()
        out += block
        counter += 1
    return bytes(out[:length])


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def _tag(mac_key: bytes, version: int, nonce: bytes, ct: bytes, aad: bytes) -> bytes:
    return hmac.new(
        mac_key,
        _TAG_PREFIX + version.to_bytes(8, "big") + nonce + ct + aad,
        hashlib.sha256,
    ).digest()


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class KeyRing:
    """File-backed set of versioned master keys with one active version."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._keys: Dict[int, bytes] = {}
        self._active: Optional[int] = None
        if self.path.exists():
            self._load()

    @classmethod
    def create(cls, path) -> "KeyRing":
        ring = cls(path)
        if ring._keys:
            raise KeyWrapError(f"keyring already exists: {path}")
        ring.add_key()
        return ring

    def _load(self) -> None:
        data = json.loads(self.path.read_text())
        self._keys = {int(v): bytes.fromhex(k) for v, k in data["keys"].items()}
        self._active = int(data["active"])

    def _save(self) -> None:
        payload = {
            "active": self._active,
            "keys": {str(v): k.hex() for v, k in sorted(self._keys.items())},
        }
        _atomic_write(self.path, json.dumps(payload, indent=2).encode())

    def _mutate(self, fn):
        with self._lock:
            lock_path = self.path.parent / (self.path.name + ".lock")
            lock_path.touch(exist_ok=True)
            with open(lock_path, "r+b") as lock_file:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    if self.path.exists():
                        self._load()
                    result = fn()
                    self._save()
                    return result
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @property
    def active_version(self) -> int:
        with self._lock:
            if self._active is None:
                raise KeyWrapError("keyring is empty")
            return self._active

    def versions(self) -> List[int]:
        with self._lock:
            return sorted(self._keys)

    def has_version(self, version: int) -> bool:
        with self._lock:
            return int(version) in self._keys

    def key(self, version: int) -> bytes:
        with self._lock:
            try:
                return self._keys[int(version)]
            except KeyError:
                raise UnknownVersionError(
                    f"no key for version {version}; it may have been retired prematurely"
                ) from None

    def add_key(self) -> int:
        """Generate a new master key, make it active, return its version."""

        def _add():
            new_version = (max(self._keys) + 1) if self._keys else 1
            self._keys[new_version] = os.urandom(_KEY_SIZE)
            self._active = new_version
            return new_version

        return self._mutate(_add)

    def retire(self, version: int) -> None:
        """Delete a key version. Refuses to delete the active version.

        Callers must verify with can_retire_version() first; this method only
        guards the active key, not records that may still need this version.
        """
        version = int(version)

        def _retire():
            if version == self._active:
                raise KeyRetirementError(f"version {version} is the active key")
            if version not in self._keys:
                raise UnknownVersionError(f"no key for version {version}")
            del self._keys[version]

        self._mutate(_retire)


def _wrap_with(master: bytes, version: int, plaintext: bytes, aad: bytes) -> bytes:
    enc_key, mac_key = _derive(master)
    nonce = os.urandom(_NONCE_SIZE)
    ct = _xor(plaintext, _keystream(enc_key, nonce, len(plaintext)))
    tag = _tag(mac_key, version, nonce, ct, aad)
    envelope = {"v": version, "nonce": nonce.hex(), "ct": ct.hex(), "tag": tag.hex()}
    return json.dumps(envelope, separators=(",", ":")).encode()


def wrap(ring: KeyRing, plaintext: bytes, aad: bytes = b"") -> bytes:
    """Wrap plaintext under the ring's active key version."""
    version = ring.active_version
    return _wrap_with(ring.key(version), version, plaintext, aad)


def _parse_envelope(envelope: bytes) -> dict:
    try:
        obj = json.loads(envelope)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EnvelopeFormatError("envelope is not valid JSON") from exc
    if not isinstance(obj, dict):
        raise EnvelopeFormatError("envelope must be a JSON object")
    if "v" not in obj:
        raise MissingVersionError(
            "envelope has no key version; refusing to guess a key"
        )
    if not isinstance(obj["v"], int):
        raise EnvelopeFormatError("envelope version must be an integer")
    for name in ("nonce", "ct", "tag"):
        if not isinstance(obj.get(name), str):
            raise EnvelopeFormatError(f"envelope is missing field {name!r}")
    return obj


def envelope_version(envelope: bytes) -> int:
    """Return the key version of an envelope; raises if it has none."""
    return _parse_envelope(envelope)["v"]


def unwrap(ring: KeyRing, envelope: bytes, aad: bytes = b"") -> bytes:
    """Decrypt an envelope using the key selected strictly by its version."""
    obj = _parse_envelope(envelope)
    version = obj["v"]
    enc_key, mac_key = _derive(ring.key(version))
    try:
        nonce = bytes.fromhex(obj["nonce"])
        ct = bytes.fromhex(obj["ct"])
        tag = bytes.fromhex(obj["tag"])
    except ValueError as exc:
        raise EnvelopeFormatError("envelope fields must be hex strings") from exc
    expected = _tag(mac_key, version, nonce, ct, aad)
    if not hmac.compare_digest(tag, expected):
        raise IntegrityError("authentication tag mismatch")
    return _xor(ct, _keystream(enc_key, nonce, len(ct)))


@dataclass
class RotationStats:
    """Progress and outcome of a rotation campaign."""

    target_version: int
    scanned: int = 0
    rotated: int = 0
    already_current: int = 0
    failed: int = 0
    remaining_by_version: Dict[str, int] = field(default_factory=dict)
    done: bool = False
    started_at: str = ""
    finished_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RotationStats":
        return cls(**data)


def _record_files(record_dir: Path) -> List[Path]:
    return sorted(
        p for p in record_dir.iterdir() if p.is_file() and p.suffix == RECORD_SUFFIX
    )


def scan_versions(record_dir) -> Dict[str, int]:
    """Count records per key version; unparseable records count as 'corrupt'."""
    counts: Dict[str, int] = {}
    for rec in _record_files(Path(record_dir)):
        try:
            key = str(envelope_version(rec.read_bytes()))
        except KeyWrapError:
            key = "corrupt"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _load_journal(journal_path: Path) -> Optional[dict]:
    if not journal_path.exists():
        return None
    return json.loads(journal_path.read_text())


def _save_journal(journal_path: Path, journal: dict) -> None:
    _atomic_write(journal_path, json.dumps(journal, indent=2).encode())


def rotate_records(
    record_dir,
    ring: KeyRing,
    progress_callback: Optional[Callable[[str, RotationStats], None]] = None,
) -> RotationStats:
    """Re-encrypt every record in record_dir to the ring's active version.

    Interruptible and idempotent: each record is replaced atomically, and a
    journal tracks processed records. Re-running after a crash skips finished
    records; a record rotated just before a crash is recognised as already
    current. Records that fail to decrypt are left untouched, counted as
    failed, and retried on the next run.
    """
    record_dir = Path(record_dir)
    target = ring.active_version
    journal_path = record_dir / JOURNAL_NAME

    journal = _load_journal(journal_path)
    if journal is None:
        stats = RotationStats(target_version=target, started_at=_now())
        journal = {"target": target, "processed": [], "stats": stats.to_dict()}
        _save_journal(journal_path, journal)
    if journal["target"] != target:
        raise RotationStateError(
            f"journal targets v{journal['target']} but active version is v{target}; "
            f"finish the in-flight rotation or remove {journal_path}"
        )

    stats = RotationStats.from_dict(journal["stats"])
    processed = set(journal["processed"])

    for rec in _record_files(record_dir):
        if rec.name in processed:
            continue
        stats.scanned += 1
        try:
            envelope = rec.read_bytes()
            if envelope_version(envelope) == target:
                stats.already_current += 1
            else:
                plaintext = unwrap(ring, envelope)
                _atomic_write(rec, _wrap_with(ring.key(target), target, plaintext, b""))
                stats.rotated += 1
        except KeyWrapError:
            stats.failed += 1
            continue  # not marked processed: retried on the next run
        processed.add(rec.name)
        journal["processed"] = sorted(processed)
        journal["stats"] = stats.to_dict()
        _save_journal(journal_path, journal)
        if progress_callback is not None:
            progress_callback(rec.name, stats)

    stats.remaining_by_version = scan_versions(record_dir)
    stats.done = stats.failed == 0
    stats.finished_at = _now()
    journal["stats"] = stats.to_dict()
    _save_journal(journal_path, journal)
    return stats


def can_retire_version(record_dir, ring: KeyRing, version: int) -> bool:
    """A key version is safe to delete only when no record needs it and no
    corrupt record might still depend on it."""
    if version == ring.active_version:
        return False
    remaining = scan_versions(record_dir)
    return remaining.get(str(version), 0) == 0 and remaining.get("corrupt", 0) == 0


def retire_stale_keys(record_dir, ring: KeyRing) -> List[int]:
    """Retire every non-active version that no record depends on."""
    retired = []
    for version in ring.versions():
        if can_retire_version(record_dir, ring, version):
            ring.retire(version)
            retired.append(version)
    return retired


def _demo() -> None:
    demo_dir = Path(tempfile.mkdtemp(prefix="keywrap-demo-"))
    record_dir = demo_dir / "records"
    record_dir.mkdir()
    ring = KeyRing.create(demo_dir / "keyring.json")

    for i in range(5):
        _atomic_write(record_dir / f"rec-{i}{RECORD_SUFFIX}", wrap(ring, f"payload-{i}".encode()))
    print(f"wrapped 5 records with v{ring.active_version}")

    new_version = ring.add_key()
    print(f"rotated master key: active version is now v{new_version}")

    def progress(name, stats):
        print(f"  {name}: scanned={stats.scanned} rotated={stats.rotated} "
              f"already_current={stats.already_current} failed={stats.failed}")

    stats = rotate_records(record_dir, ring, progress_callback=progress)
    print("rotation stats:", json.dumps(stats.to_dict(), indent=2))

    retired = retire_stale_keys(record_dir, ring)
    print(f"retired key versions: {retired}; remaining versions: {ring.versions()}")

    for rec in _record_files(record_dir):
        unwrap(ring, rec.read_bytes())
    print("all records still decrypt after retirement")
    print(f"demo artifacts in {demo_dir}")


if __name__ == "__main__":
    _demo()
