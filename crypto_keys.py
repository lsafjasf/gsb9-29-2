"""HMAC and context-separated HKDF key derivation.

Only Python's standard library is used.  HMAC is implemented directly on the
block-hash primitive rather than delegating to the ``hmac`` module.
"""

from __future__ import annotations

import hashlib
import hmac as _stdlib_compare
from typing import SupportsBytes


SUPPORTED_HASHES = ("sha256", "sha384", "sha512")


class KeyMaterialError(ValueError):
    """Raised when a key or key-derivation argument is invalid."""


class InvalidMACError(ValueError):
    """Raised when a message authentication tag does not verify."""


BytesLike = bytes | bytearray | memoryview | SupportsBytes


def _copy_bytes(value: BytesLike, name: str) -> bytearray:
    if isinstance(value, (bytes, bytearray, memoryview)):
        view = memoryview(value)
        if view.format != "B" or view.ndim != 1:
            raise TypeError(f"{name} must contain contiguous bytes")
        return bytearray(view)
    if hasattr(value, "__bytes__"):
        return bytearray(bytes(value))
    raise TypeError(f"{name} must be bytes-like")


def _hashlib_hash(algorithm: str):
    if algorithm not in SUPPORTED_HASHES:
        raise ValueError(f"unsupported hash: {algorithm!r}")
    return hashlib.new(algorithm)


def zeroize(buffer: bytearray | None) -> None:
    """Best-effort explicit clearing of a mutable byte buffer."""
    if buffer is not None:
        for index in range(len(buffer)):
            buffer[index] = 0


class SecretBytes:
    """Mutable key material that can be explicitly overwritten.

    Python cannot guarantee that all copies are erased because bytes objects,
    hash implementations, the allocator, and the OS may retain data.  This type
    nevertheless gives callers an explicit point to clear material they own.
    """

    _data: bytearray

    def __init__(self, data: BytesLike):
        self._data = _copy_bytes(data, "data")
        self._cleared = False
    @property
    def data(self) -> bytearray:
        if self._cleared:
            raise ValueError("secret has been cleared")
        return self._data

    def reveal(self) -> bytes:
        """Return an immutable copy."""
        return bytes(self.data)

    def hex(self) -> str:
        return self.data.hex()

    def __len__(self) -> int:
        return len(self._data)

    def __bytes__(self) -> bytes:
        return self.reveal()

    def __eq__(self, other: object) -> bool:
        if self._cleared or not isinstance(other, SecretBytes) or other._cleared:
            return False
        return _stdlib_compare.compare_digest(bytes(self._data), bytes(other._data))

    def clear(self) -> None:
        zeroize(self._data)
        self._cleared = True

    @property
    def cleared(self) -> bool:
        return self._cleared

    def __enter__(self) -> "SecretBytes":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.clear()

    def __del__(self) -> None:
        try:
            if not getattr(self, "_cleared", True):
                self.clear()
        except Exception:
            pass


class HMAC:
    """Incremental HMAC using a block hash.

    HMAC does not expose ``hash(key || message)``.  Its nested-key construction
    prevents the length-extension attacks that affect plain Merkle-Damgard
    hashes such as SHA-256.
    """

    def __init__(self, key: BytesLike, message: BytesLike | None = None, algorithm: str = "sha256"):
        self.algorithm = algorithm
        hash_object = _hashlib_hash(algorithm)
        self.digest_size = hash_object.digest_size
        block_size = hash_object.block_size

        adjusted_key = _copy_bytes(key, "key")
        try:
            if len(adjusted_key) > block_size:
                key_hash = _hashlib_hash(algorithm)
                key_hash.update(adjusted_key)
                hashed_key = bytearray(key_hash.digest())
                zeroize(adjusted_key)
                adjusted_key = hashed_key
            if len(adjusted_key) < block_size:
                adjusted_key.extend(b"\x00" * (block_size - len(adjusted_key)))

            self._inner_pad = bytearray(block_size)
            self._outer_pad = bytearray(block_size)
            for index, byte in enumerate(adjusted_key):
                self._inner_pad[index] = byte ^ 0x36
                self._outer_pad[index] = byte ^ 0x5C
        finally:
            zeroize(adjusted_key)

        self._inner = _hashlib_hash(algorithm)
        self._inner.update(self._inner_pad)
        self._cleared = False
        if message is not None:
            self.update(message)

    def update(self, message: BytesLike) -> "HMAC":
        if self._cleared:
            raise ValueError("HMAC object has been cleared")
        view = memoryview(message)
        if view.format != "B" or view.ndim != 1:
            raise TypeError("message must contain contiguous bytes")
        self._inner.update(view)
        return self

    def digest(self, into: bytearray | None = None) -> bytes | None:
        if self._cleared:
            raise ValueError("HMAC object has been cleared")
        inner_digest = self._inner.digest()
        outer = _hashlib_hash(self.algorithm)
        outer.update(self._outer_pad)
        outer.update(inner_digest)
        if into is None:
            return outer.digest()
        output = outer.digest()
        into.clear()
        into.extend(output)
        return

    def hexdigest(self) -> str:
        return self.digest().hex()

    def clear(self) -> None:
        zeroize(self._inner_pad)
        zeroize(self._outer_pad)
        self._inner = _hashlib_hash(self.algorithm)
        self._cleared = True

    @property
    def cleared(self) -> bool:
        return self._cleared

    def __enter__(self) -> "HMAC":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.clear()

    def __del__(self) -> None:
        try:
            self.clear()
        except Exception:
            pass


def hmac_digest(
    key: BytesLike,
    message: BytesLike,
    algorithm: str = "sha256",
    into: bytearray | None = None,
) -> bytes | None:
    state = HMAC(key, message, algorithm=algorithm)
    if into is None:
        return state.digest()
    try:
        state.digest(into)
        return None
    finally:
        state.clear()


def compare_digest(left: BytesLike, right: BytesLike) -> bool:
    if isinstance(left, SecretBytes):
        left = bytes(left.data)
    if isinstance(right, SecretBytes):
        right = bytes(right.data)
    return _stdlib_compare.compare_digest(left, right)


def verify_mac(
    key: BytesLike,
    message: BytesLike,
    tag: BytesLike,
    algorithm: str = "sha256",
) -> bool:
    expected = hmac_digest(key, message, algorithm)
    return _stdlib_compare.compare_digest(expected, bytes(tag))


def verify_mac_or_raise(
    key: BytesLike,
    message: BytesLike,
    tag: BytesLike,
    algorithm: str = "sha256",
) -> None:
    if not verify_mac(key, message, tag, algorithm):
        raise InvalidMACError("message authentication failed")


def hkdf_extract(
    salt: BytesLike | None,
    input_key_material: BytesLike,
    algorithm: str = "sha256",
    into: bytearray | None = None,
) -> bytes | None:
    hash_size = _hashlib_hash(algorithm).digest_size
    prk = bytearray(hash_size)
    if salt is None or len(salt) == 0:
        salt_value = bytearray(hash_size)
        owns_salt = True
    else:
        salt_value = _copy_bytes(salt, "salt")
        owns_salt = False
    try:
        hmac_digest(salt_value, input_key_material, algorithm, into=prk)
        if into is None:
            return bytes(prk)
        into.clear()
        into.extend(prk)
        return None
    finally:
        zeroize(prk)
        zeroize(salt_value)


def hkdf_expand(
    pseudo_random_key: BytesLike,
    info: BytesLike | None = b"",
    output_length: int = 32,
    algorithm: str = "sha256",
    into: bytearray | None = None,
) -> bytes | None:
    if not isinstance(output_length, int) or isinstance(output_length, bool):
        raise TypeError("output_length must be an integer")
    if output_length <= 0:
        raise ValueError("output_length must be positive")

    hash_size = _hashlib_hash(algorithm).digest_size
    max_length = 255 * hash_size
    if output_length > max_length:
        raise ValueError(f"output_length cannot exceed {max_length} bytes")
    if len(pseudo_random_key) < hash_size:
        raise KeyMaterialError(f"PRK must be at least {hash_size} bytes")

    info_value = bytearray(b"" if info is None else bytes(info))
    result = bytearray()
    previous = bytearray()
    chunk = bytearray(hash_size)
    hmac_input = bytearray()
    block_number = 1
    output = None
    try:
        while len(result) < output_length:
            hmac_input.clear()
            hmac_input.extend(previous)
            hmac_input.extend(info_value)
            hmac_input.append(block_number)
            hmac_digest(pseudo_random_key, hmac_input, algorithm, into=chunk)
            result.extend(chunk)
            previous[:] = chunk
            block_number += 1
        if into is None:
            output = bytes(result[:output_length])
            return output
        into.clear()
        into.extend(result[:output_length])
        return
    finally:
        zeroize(chunk)
        zeroize(previous)
        zeroize(hmac_input)
        zeroize(info_value)
        zeroize(result)


def _append_field(target: bytearray, value: bytes) -> None:
    length = len(value)
    if length > 0xFFFFFFFF:
        raise ValueError("field is too long")
    target.extend(length.to_bytes(4, "big"))
    target.extend(value)


class KeyDeriver:
    """Derive domain-separated subkeys from one master key with HKDF."""

    def __init__(
        self,
        master_key: BytesLike,
        salt: BytesLike | None = None,
        algorithm: str = "sha256",
    ):
        if len(master_key) == 0:
            raise KeyMaterialError("master key must not be empty")
        self.algorithm = algorithm
        self._prk = bytearray(_hashlib_hash(algorithm).digest_size)
        self._cleared = True
        try:
            hkdf_extract(salt, master_key, algorithm, into=self._prk)
            self._cleared = False
        except BaseException:
            self.clear()
            raise

    def derive(
        self,
        length: int,
        *,
        context: BytesLike,
        label: BytesLike | None = b"",
        info: BytesLike | None = b"",
    ) -> SecretBytes:
        if self._cleared:
            raise ValueError("KeyDeriver has been cleared")
        context_value = bytes(context)
        if not context_value:
            raise KeyMaterialError("context must be non-empty")
        if not isinstance(length, int) or isinstance(length, bool) or length <= 0:
            raise ValueError("length must be a positive integer")

        domain_info = bytearray()
        try:
            _append_field(domain_info, b"subkey-derivation-v1")
            _append_field(domain_info, self.algorithm.encode("ascii"))
            _append_field(domain_info, length.to_bytes(4, "big"))
            _append_field(domain_info, context_value)
            _append_field(domain_info, b"" if label is None else bytes(label))
            _append_field(domain_info, b"" if info is None else bytes(info))
            output = bytearray(length)
            hkdf_expand(
                self._prk,
                domain_info,
                length,
                self.algorithm,
                into=output,
            )
            return SecretBytes(output)
        finally:
            zeroize(locals().get("output"))
            zeroize(domain_info)

    def clear(self) -> None:
        zeroize(self._prk)
        self._cleared = True

    @property
    def cleared(self) -> bool:
        return self._cleared

    def __enter__(self) -> "KeyDeriver":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.clear()

    def __del__(self) -> None:
        try:
            if not getattr(self, "_cleared", True):
                self.clear()
        except Exception:
            pass
