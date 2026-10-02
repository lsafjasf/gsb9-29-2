"""NP1: a small typed, length-prefixed nested binary protocol used as the fuzzing target.

Wire format::

    packet  = MAGIC(2) VERSION(1) FLAGS(1) COUNT(u16-be) NODE{COUNT}
    node    = TAG(1) LEN(u32-be) PAYLOAD[LEN]
    TAG 0x01 U32     PAYLOAD = VALUE(u32-be) (LEN must be 4)
    TAG 0x02 BLOB    PAYLOAD = raw bytes
    TAG 0x03 LIST    PAYLOAD = NODE{LEN}                 (children bounded by byte length)
    TAG 0x04 STRUCT  PAYLOAD = CCOUNT(u16-be) NODE{CCOUNT} (LEN is the byte length of PAYLOAD)

Every mutation operates on this documented structure rather than on random bytes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Callable, Dict, List

MAGIC = 0x4E50          # "NP"
VERSION = 1
FLAG_LAST = 0x01

TAG_U32 = 0x01
TAG_BLOB = 0x02
TAG_LIST = 0x03
TAG_STRUCT = 0x04
TAGS = (TAG_U32, TAG_BLOB, TAG_LIST, TAG_STRUCT)


@dataclass
class Node:
    tag: int
    value: object = None          # int (U32) | bytes (BLOB) | list[Node]
    pin_len: int = -1             # >= 0: serialize this LEN field verbatim
    pin_cval: int = -1            # >= 0: STRUCT: serialize CCOUNT verbatim


@dataclass
class Document:
    flags: int = 0
    children: List[Node] = field(default_factory=list)
    pin_count: int = -1           # >= 0: serialize the root COUNT field verbatim


# --------------------------------------------------------------------------
# serialization
# --------------------------------------------------------------------------

def payload_len(node: Node) -> int:
    if node.tag == TAG_U32:
        return 4
    if node.tag == TAG_BLOB:
        return len(node.value)
    if node.tag == TAG_LIST:
        return sum(_node_len(c) for c in node.value)
    if node.tag == TAG_STRUCT:
        return 2 + sum(_node_len(c) for c in node.value)
    raise ValueError("unknown tag 0x%02x" % node.tag)


def _node_len(node: Node) -> int:
    return 1 + 4 + payload_len(node)


def serialize_doc(doc: Document) -> bytes:
    count = len(doc.children) if doc.pin_count < 0 else doc.pin_count
    out = struct.pack(">HBBH", MAGIC, VERSION, doc.flags, min(count, 0xFFFF))
    for child in doc.children:
        out += serialize_node(child)
    return out


def serialize_node(node: Node) -> bytes:
    declared = payload_len(node) if node.pin_len < 0 else node.pin_len
    out = struct.pack(">BI", node.tag, min(declared, 0xFFFFFFFF))
    if node.tag == TAG_U32:
        out += struct.pack(">I", node.value & 0xFFFFFFFF)
    elif node.tag == TAG_BLOB:
        out += node.value
    elif node.tag == TAG_LIST:
        for child in node.value:
            out += serialize_node(child)
    elif node.tag == TAG_STRUCT:
        ccount = len(node.value) if node.pin_cval < 0 else node.pin_cval
        out += struct.pack(">H", min(ccount, 0xFFFF))
        for child in node.value:
            out += serialize_node(child)
    else:
        raise ValueError("unknown tag 0x%02x" % node.tag)
    return out


# --------------------------------------------------------------------------
# valid seed corpus
# --------------------------------------------------------------------------

def _u32(v: int) -> Node:
    return Node(TAG_U32, v)


def _blob(b: bytes) -> Node:
    return Node(TAG_BLOB, bytearray(b))


def _list(children: List[Node]) -> Node:
    return Node(TAG_LIST, list(children))


def _struct(children: List[Node]) -> Node:
    return Node(TAG_STRUCT, list(children))


def build_small() -> Document:
    return Document(flags=0, children=[
        _u32(7),
        _blob(b"abc"),
        _struct([_u32(11), _blob(b"xy")]),
    ])


def build_medium() -> Document:
    return Document(flags=FLAG_LAST, children=[
        _u32(1),
        _list([
            _u32(2),
            _struct([_blob(b"aa"), _u32(3)]),
            _blob(b"bb"),
        ]),
        _struct([
            _list([_u32(4), _u32(5)]),
            _blob(b"cc"),
        ]),
        _blob(b"dd"),
    ])


def build_header_only() -> Document:
    return Document(flags=0, children=[])


def build_deep(depth: int) -> Document:
    node = _u32(0xC0DE)
    for _ in range(depth):
        node = _list([node])
    return Document(flags=FLAG_LAST, children=[node, _blob(b"tail")])


def build_wide(width: int) -> Document:
    return Document(flags=0, children=[
        _struct([_u32(i & 0xFFFFFFFF) for i in range(width)])
    ])


def build_blob(size: int) -> Document:
    return Document(flags=0, children=[_blob(b"A" * size)])


def clone_doc(doc: Document) -> Document:
    def clone_node(node: Node) -> Node:
        if node.tag in (TAG_LIST, TAG_STRUCT):
            return Node(node.tag, [clone_node(c) for c in node.value],
                        node.pin_len, node.pin_cval)
        if node.tag == TAG_BLOB:
            return Node(node.tag, bytearray(node.value), node.pin_len,
                        node.pin_cval)
        return Node(node.tag, node.value, node.pin_len, node.pin_cval)

    return Document(doc.flags, [clone_node(c) for c in doc.children], doc.pin_count)


def tree_depth(doc: Document) -> int:
    def depth(node: Node) -> int:
        if node.tag in (TAG_LIST, TAG_STRUCT):
            inner = [depth(c) for c in node.value]
            return 1 + (max(inner) if inner else 0)
        return 1

    return max((depth(c) for c in doc.children), default=0)


SEED_BUILDERS: Dict[str, Callable[[], Document]] = {
    "small": build_small,
    "medium": build_medium,
    "header_only": build_header_only,
    "deep": lambda: build_deep(20),
    "wide": lambda: build_wide(24),
    "blob": lambda: build_blob(2048),
}
