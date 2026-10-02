"""System under test: an intentionally imperfect NP1 parser.

This parser is written in a realistic "trust the wire" style and contains four
latent defects that a structure-aware fuzzer should be able to reach.  Normal,
conformant NP1 packets parse cleanly.  Expected rejections raise
``ProtocolError``; the defects manifest as other exception classes or as hangs.

Defects
-------
1. container length/count fields are trusted verbatim -> ``AssertionError``
   on desynchronised structures;
2. nesting depth is unbounded -> ``RecursionError`` on deep packets;
3. a historical magic value (``0xDEADBEEF``) keeps a legacy switch loop alive
   forever -> timeout;
4. a BLOB length above the allocation ceiling triggers ``MemoryError`` instead
   of a clean rejection.
"""

from __future__ import annotations

import struct

from .protocol import (
    FLAG_LAST,
    MAGIC,
    TAG_BLOB,
    TAG_LIST,
    TAG_STRUCT,
    TAG_U32,
    VERSION,
)

MAX_BLOB = 4 * 1024 * 1024
MAX_DEPTH = 800

LEGACY_SENTINEL = 0xDEADBEEF


class ProtocolError(Exception):
    """Raised for messages the parser is allowed to reject."""


class Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ProtocolError("truncated input at %d" % self.pos)
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return struct.unpack(">H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack(">I", self.take(4))[0]


def _legacy_compat(value: int) -> None:
    # Defect: a legacy compatibility switch that never advances when the
    # historical sentinel value appears on the wire.
    while value == LEGACY_SENTINEL:
        value = value  # pragma: no hang - only reachable for the sentinel


def parse_message(data: bytes):
    reader = Reader(data)
    magic = reader.u16()
    version = reader.u8()
    flags = reader.u8()
    if magic != MAGIC:
        raise ProtocolError("bad magic 0x%04x" % magic)
    if version != VERSION:
        raise ProtocolError("bad version %d" % version)
    if flags & ~(FLAG_LAST) != 0:
        raise ProtocolError("bad flags 0x%02x" % flags)

    count = reader.u16()
    return [_parse_node(reader, 1) for _ in range(count)]


def _parse_node(reader: Reader, depth: int):
    if depth > MAX_DEPTH:
        # Defect: nesting is effectively only bounded by the Python stack.
        raise RecursionError("nesting exceeds parser stack")

    tag = reader.u8()
    length = reader.u32()
    end = reader.pos + length

    if tag == TAG_U32:
        assert length == 4, "u32 length must be 4, got %d" % length
        value = reader.u32()
        assert reader.pos == end, "u32 desync"
        _legacy_compat(value)
        return ("u32", value)

    if tag == TAG_BLOB:
        if length > MAX_BLOB:
            raise MemoryError("cannot preallocate blob of %d bytes" % length)
        value = bytes(reader.take(length))
        return ("blob", value)

    if tag == TAG_LIST:
        children = []
        while reader.pos < end:
            children.append(_parse_node(reader, depth + 1))
        # Defect: trusts the declared length instead of treating the mismatch
        # as a recoverable protocol error.
        assert reader.pos == end, "list length desync (declared %d)" % length
        return ("list", children)

    if tag == TAG_STRUCT:
        ccount = reader.u16()
        children = [_parse_node(reader, depth + 1) for _ in range(ccount)]
        assert reader.pos == end, "struct length desync (declared %d)" % length
        return ("struct", children)

    raise ProtocolError("unknown tag 0x%02x" % tag)
