"""Versioned key wrapping with interruptible, resumable rotation.

Stdlib-only. Construction:
  - Per-version 256-bit master key stored in a KeyStore.
  - HKDF-SHA256(master) -> (enc_key, mac_key).
  - Encryption: HMAC-SHA256 keystream in counter mode (stream cipher).
  - Integrity:  Encrypt-then-MAC; the HMAC tag covers the header
    (version + algorithm), nonce and ciphertext, so version tampering
    is detected as an integrity failure.

Envelope format (JSON, all binary fields base64):
  {"alg": "HMAC-CTR-SHA256", "v": 2, "nonce": "...", "ct": "...", "tag": "..."}

Rotation model:
  1. begin_rotation()  - create + activate the new key. New writes use it.
  2. rotate()          - re-wrap records one by one with atomic replace.
                         Each record carries its own version, so the dataset
                         is decryptable at every interruption point.
  3. retire_key()      - only succeeds when no record references the old
                         version (checked against the store).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import threading
from dataclasses import dataclass

ALG = "HMAC-CTR-SHA256"
KEY_LEN = 32
NONCE_LEN = 16


# ---------------------------------------------------------------- errors

class KeyWrapError(Exception):
    """Base class for all keywrap failures."""


class MissingVersionError(KeyWrapError):
    """Envelope has no key version; we refuse to guess."""


class UnknownKeyVersionError(KeyWrapError):
    """Envelope references a version not present in the KeyStore."""


class IntegrityError(KeyWrapError):
    """Authentication tag mismatch (tampered or corrupted envelope)."""


# ---------------------------------------------------------------- helpers

def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise KeyWrapError(f"invalid base64 field: {exc}") from exc


def _hkdf(master: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(b"keywrap-hkdf-salt-v1", master, hashlib.sha256).digest()
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def _subkeys(master: bytes) -> tuple[bytes, bytes]:
    material = _hkdf(master, b"keywrap-subkeys", 2 * KEY_LEN)
    return material[:KEY_LEN], material[KEY_LEN:]


def _keystream(enc_key: bytes, nonce: bytes, nbytes: int) -> bytes:
    out, counter = b"", 0
    while len(out) < nbytes:
        out += hmac.new(enc_key, nonce + struct.pack(">Q", counter), hashlib.sha256).digest()
        counter += 1
    return out[:nbytes]


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def _canonical_header(version: int) -> bytes:
    return json.dumps({"alg": ALG, "v": version}, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _tag(mac_key: bytes, version: int, nonce: bytes, ct: bytes) -> bytes:
    return hmac.new(mac_key, _canonical_header(version) + nonce + ct, hashlib.sha256).digest()


# ---------------------------------------------------------------- key store

class KeyStore:
    """Thread-safe versioned key store with optional JSON-file persistence."""

    def __init__(self, path: str | None = None):
        self._lock = threading.RLock()
        self._keys: dict[int, bytes] = {}
        self._active: int | None = None
        self._path = path
        if path and os.path.exists(path):
            self._load(path)

    # -- key lifecycle -------------------------------------------------

    def create_key(self, version: int | None = None, *, activate: bool = True) -> int:
        with self._lock:
            if version is None:
                version = max(self._keys, default=0) + 1
            if version in self._keys:
                raise KeyWrapError(f"key version {version} already exists")
            self._keys[version] = secrets.token_bytes(KEY_LEN)
            if activate or self._active is None:
                self._active = version
            self._save()
            return version

    def import_key(self, version: int, key: bytes, *, activate: bool = False) -> None:
        if len(key) != KEY_LEN:
            raise KeyWrapError(f"key must be {KEY_LEN} bytes")
        with self._lock:
            if version in self._keys:
                raise KeyWrapError(f"key version {version} already exists")
            self._keys[version] = bytes(key)
            if activate or self._active is None:
                self._active = version
            self._save()

    def set_active(self, version: int) -> None:
        with self._lock:
            if version not in self._keys:
                raise UnknownKeyVersionError(f"no key for version {version}")
            self._active = version
            self._save()

    def retire(self, version: int) -> None:
        """Delete a key. Refuses to delete the active key.

        Deleting a key that still has data is allowed here (disaster
        recovery may require it) but unwrap() of that data will then
        raise UnknownKeyVersionError. Use retire_key() for the safe path.
        """
        with self._lock:
            if version == self._active:
                raise KeyWrapError(f"cannot retire active key version {version}")
            if version not in self._keys:
                raise UnknownKeyVersionError(f"no key for version {version}")
            del self._keys[version]
            self._save()

    # -- queries --------------------------------------------------------

    @property
    def active_version(self) -> int | None:
        with self._lock:
            return self._active

    def versions(self) -> list[int]:
        with self._lock:
            return sorted(self._keys)

    def has(self, version: int) -> bool:
        with self._lock:
            return version in self._keys

    def get(self, version: int) -> bytes:
        with self._lock:
            try:
                return self._keys[version]
            except KeyError:
                raise UnknownKeyVersionError(
                    f"no key for version {version} (known: {sorted(self._keys)})"
                ) from None

    # -- persistence ----------------------------------------------------

    def _save(self) -> None:
        if not self._path:
            return
        payload = {
            "active": self._active,
            "keys": {str(v): _b64e(k) for v, k in sorted(self._keys.items())},
        }
        tmp = f"{self._path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path)  # atomic: no torn keystore on crash

    def _load(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        self._keys = {int(v): _b64d(k) for v, k in payload["keys"].items()}
        self._active = payload["active"]


# ---------------------------------------------------------------- wrap / unwrap

def wrap(data: bytes, keystore: KeyStore, version: int | None = None) -> bytes:
    """Encrypt `data` under `version` (default: the active key)."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("wrap() expects bytes")
    data = bytes(data)
    if version is None:
        version = keystore.active_version
        if version is None:
            raise KeyWrapError("no active key version")
    enc_key, mac_key = _subkeys(keystore.get(version))
    nonce = secrets.token_bytes(NONCE_LEN)
    ct = _xor(data, _keystream(enc_key, nonce, len(data)))
    envelope = {
        "alg": ALG,
        "v": version,
        "nonce": _b64e(nonce),
        "ct": _b64e(ct),
        "tag": _b64e(_tag(mac_key, version, nonce, ct)),
    }
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _parse_envelope(envelope: bytes) -> dict:
    try:
        env = json.loads(envelope)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise KeyWrapError(f"malformed envelope: {exc}") from exc
    if not isinstance(env, dict):
        raise KeyWrapError("malformed envelope: not a JSON object")
    return env


def peek_version(envelope: bytes) -> int:
    """Read the key version without decrypting. Missing version -> error."""
    env = _parse_envelope(envelope)
    if "v" not in env:
        raise MissingVersionError("envelope has no key version field 'v'")
    version = env["v"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise KeyWrapError(f"invalid key version: {version!r}")
    return version


def unwrap(envelope: bytes, keystore: KeyStore) -> bytes:
    """Decrypt an envelope, selecting the key strictly by its version field."""
    env = _parse_envelope(envelope)
    version = peek_version(envelope)  # raises MissingVersionError if absent
    if env.get("alg") != ALG:
        raise KeyWrapError(f"unsupported algorithm: {env.get('alg')!r}")
    for field in ("nonce", "ct", "tag"):
        if not isinstance(env.get(field), str):
            raise KeyWrapError(f"missing or invalid field {field!r}")
    nonce, ct, tag = _b64d(env["nonce"]), _b64d(env["ct"]), _b64d(env["tag"])
    enc_key, mac_key = _subkeys(keystore.get(version))  # UnknownKeyVersionError
    expected = _tag(mac_key, version, nonce, ct)
    if not hmac.compare_digest(tag, expected):
        raise IntegrityError(f"tag mismatch (version {version})")
    return _xor(ct, _keystream(enc_key, nonce, len(ct)))


# ---------------------------------------------------------------- record store

_RID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*")


class RecordStore:
    """Directory-backed record store; one file per record, atomic replace.

    put() writes to a temp file and os.replace()s it, so a crash mid-write
    never leaves a torn record - readers see either the old or new envelope.
    """

    def __init__(self, path: str):
        self.path = path
        os.makedirs(path, exist_ok=True)

    def _fp(self, rid: str) -> str:
        if not _RID_RE.fullmatch(rid):
            raise KeyWrapError(f"invalid record id: {rid!r}")
        return os.path.join(self.path, rid)

    def put(self, rid: str, data: bytes) -> None:
        fp = self._fp(rid)
        tmp = f"{fp}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, fp)

    def get(self, rid: str) -> bytes:
        with open(self._fp(rid), "rb") as fh:
            return fh.read()

    def ids(self) -> list[str]:
        return sorted(n for n in os.listdir(self.path) if ".tmp." not in n)

    def __len__(self) -> int:
        return len(self.ids())


# ---------------------------------------------------------------- rotation

@dataclass
class RotationStats:
    total: int = 0
    reencrypted: int = 0        # records moved to the target version
    already_current: int = 0    # records already on the target version
    failed: int = 0             # records that could not be re-wrapped
    bytes_reencrypted: int = 0
    interrupted: bool = False

    @property
    def processed(self) -> int:
        return self.reencrypted + self.already_current

    @property
    def remaining(self) -> int:
        return self.total - self.processed - self.failed

    def summary(self, target_version: int) -> str:
        state = "INTERRUPTED" if self.interrupted else "COMPLETE"
        return (
            f"[{state}] target=v{target_version} total={self.total} "
            f"reencrypted={self.reencrypted} already_current={self.already_current} "
            f"failed={self.failed} remaining={self.remaining} "
            f"bytes_reencrypted={self.bytes_reencrypted}"
        )


def begin_rotation(keystore: KeyStore) -> int:
    """Phase 1: create and activate the next key version.

    After this call, new writes use the new key while every existing
    envelope remains decryptable under its own recorded version.
    """
    return keystore.create_key(activate=True)


def rotate(store: RecordStore, keystore: KeyStore,
          target_version: int | None = None,
          should_stop=None, on_progress=None) -> RotationStats:
    """Phase 2: re-wrap every record to `target_version` (default: active).

    Resumable: records already on the target version are skipped, so
    re-running after an interruption continues where it left off.
    `should_stop()` is polled before each record; returning True simulates
    a crash/kill (the current record is left untouched).
    """
    if target_version is None:
        target_version = keystore.active_version
    if target_version is None or not keystore.has(target_version):
        raise UnknownKeyVersionError(f"no key for target version {target_version}")

    stats = RotationStats()
    ids = store.ids()
    stats.total = len(ids)
    for index, rid in enumerate(ids):
        if should_stop is not None and should_stop():
            stats.interrupted = True
            break
        envelope = store.get(rid)
        if peek_version(envelope) == target_version:
            stats.already_current += 1
        else:
            try:
                data = unwrap(envelope, keystore)
                store.put(rid, wrap(data, keystore, version=target_version))
                stats.reencrypted += 1
                stats.bytes_reencrypted += len(data)
            except KeyWrapError:
                stats.failed += 1
        if on_progress is not None:
            on_progress(stats, index + 1, rid)
    return stats


def count_by_version(store: RecordStore) -> dict[int | None, int]:
    """Count envelopes per key version; malformed ones count under None."""
    counts: dict[int | None, int] = {}
    for rid in store.ids():
        try:
            version: int | None = peek_version(store.get(rid))
        except KeyWrapError:
            version = None
        counts[version] = counts.get(version, 0) + 1
    return counts


def remaining_on_version(store: RecordStore, version: int) -> int:
    """How many records still need `version`. 0 == old key is safe to drop."""
    counts = count_by_version(store)
    return counts.get(version, 0) + counts.get(None, 0)


def retire_key(keystore: KeyStore, store: RecordStore, version: int) -> None:
    """Phase 3: delete an old key, but only once nothing references it."""
    remaining = remaining_on_version(store, version)
    if remaining:
        raise KeyWrapError(
            f"refusing to retire v{version}: {remaining} record(s) still need it"
        )
    keystore.retire(version)
