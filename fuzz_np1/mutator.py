"""Structure-aware mutator for NP1 packets.

Every mutation is a plain, JSON-serialisable *spec* tuple, e.g.
``("len_pin_delta", 3, -1)`` or ``("reorder", 0, "reverse")``.  Specs act on the
typed :class:`~fuzz_np1.protocol.Document` tree, so mutated packets keep the
shape the parser expects and reach deep branches.

Mutation families
-----------------
* ``len_pin_*``    - tamper node LEN fields (absolute boundary values / deltas)
* ``count_pin_*``  - tamper a STRUCT child count or the root COUNT
* ``u32_boundary`` - replace a U32 value with boundary values
* ``blob_*``       - replace BLOB contents with boundary-shaped payloads
* ``reorder``      - swap / reverse / rotate children of a container
* ``wrap``/``flatten``/``drop``/``insert`` - nesting hierarchy edits
* wire-level ops   - truncate / append / duplicate / byte-flip on serialised bytes

All variants are produced from deterministic enumeration or from a seeded
``random.Random`` instance, so a (seed, sample) pair always yields the same
sequence.
"""

from __future__ import annotations

import random
from typing import Iterator, List, Optional, Tuple

from .protocol import (
    Document,
    Node,
    TAG_BLOB,
    TAG_LIST,
    TAG_STRUCT,
    TAG_U32,
    clone_doc,
    payload_len,
    serialize_doc,
)

Spec = Tuple

# --------------------------------------------------------------------------
# parameter tables
# --------------------------------------------------------------------------

LEN_ABS_VALUES = (0, 1, 0x7F, 0xFF, 0xFFFF, 0x10000, 0xFFFFFFFF)
LEN_DELTAS = (-16, -1, 1, 16)

U32_BOUNDS = (
    0, 1, 0x7F, 0x80, 0xFF, 0x100, 0x7FFF, 0xFFFF, 0x10000,
    0x7FFFFFFF, 0x80000000, 0xFFFFFFFE, 0xFFFFFFFF,
)

BLOB_PATTERNS = (
    b"",
    b"\x00" * 8,
    b"\xff" * 8,
    b"\x01\x00\x00\x00",                 # looks like a tiny U32 node
    b"NP\x01\x00",
    b"A" * 256,
)

REORDER_MODES = ("swap01", "reverse", "rotate")

APPEND_PATTERNS = (b"\x00", b"\xff", b"NP", b"\x03\x00\x00\x00")

CONTAINER_TAGS = (TAG_LIST, TAG_STRUCT)


# --------------------------------------------------------------------------
# tree helpers (indices are pre-order positions; -1 denotes the document root)
# --------------------------------------------------------------------------

def all_nodes(doc: Document) -> List[Node]:
    out: List[Node] = []

    def walk(node: Node) -> None:
        out.append(node)
        if node.tag in CONTAINER_TAGS:
            for child in node.value:
                walk(child)

    for child in doc.children:
        walk(child)
    return out


def _locate(doc: Document, index: int) -> Tuple[Optional[List[Node]], int]:
    """Return (parent_child_list, position) for pre-order ``index``."""
    if index < 0 or index >= len(all_nodes(doc)):
        return None, -1

    def walk(node: Node, parent_list: List[Node], pos: int) -> bool:
        if walk.counter == index:
            walk.slot = (parent_list, pos)
            return True
        walk.counter += 1
        if node.tag in CONTAINER_TAGS:
            for i, child in enumerate(node.value):
                if walk(child, node.value, i):
                    return True
        return False

    walk.counter = 0
    walk.slot = None
    for i, child in enumerate(doc.children):
        if walk(child, doc.children, i):
            break
    return walk.slot if walk.slot is not None else (None, -1)


def _node_at(doc: Document, index: int) -> Optional[Node]:
    nodes = all_nodes(doc)
    return nodes[index] if 0 <= index < len(nodes) else None


def _fresh(tag: int) -> Node:
    if tag == TAG_U32:
        return Node(TAG_U32, 0)
    if tag == TAG_BLOB:
        return Node(TAG_BLOB, bytearray(b"X"))
    if tag == TAG_LIST:
        return Node(TAG_LIST, [Node(TAG_U32, 0)])
    if tag == TAG_STRUCT:
        return Node(TAG_STRUCT, [Node(TAG_U32, 0)])
    raise ValueError(tag)


# --------------------------------------------------------------------------
# spec enumeration (deterministic order)
# --------------------------------------------------------------------------

def enumerate_doc_specs(doc: Document) -> List[Spec]:
    """Every applicable structured mutation for ``doc``, in a fixed order."""
    specs: List[Spec] = []
    nodes = all_nodes(doc)
    container_count = len(doc.children)

    for idx, node in enumerate(nodes):
        real_len = payload_len(node)
        for value in LEN_ABS_VALUES:
            specs.append(("len_pin_abs", idx, value))
        for delta in LEN_DELTAS:
            specs.append(("len_pin_delta", idx, real_len + delta))

        if node.tag == TAG_U32:
            for value in U32_BOUNDS:
                specs.append(("u32_boundary", idx, value))
        elif node.tag == TAG_BLOB:
            for pat_idx in range(len(BLOB_PATTERNS)):
                specs.append(("blob_pattern", idx, pat_idx))
        else:
            for mode in REORDER_MODES:
                specs.append(("reorder", idx, mode))
            for value in (0, max(node.value and len(node.value) - 1, 0),
                          len(node.value) + 1, 0xFFFF):
                specs.append(("count_pin_abs", idx, value))
    for value in (0, max(container_count - 1, 0),
                  container_count + 1, 0xFFFF):
        specs.append(("count_pin_abs", -1, value))
    for idx, node in enumerate(nodes):
        specs.append(("wrap", idx, TAG_LIST))
        specs.append(("wrap", idx, TAG_STRUCT))
        specs.append(("insert", idx, TAG_U32))
        specs.append(("insert", idx, TAG_BLOB))
        specs.append(("drop", idx, 0))
        if node.tag in CONTAINER_TAGS and node.value:
            specs.append(("flatten", idx, 0))

    return specs


def enumerate_wire_specs(data: bytes) -> List[Spec]:
    specs: List[Spec] = []
    for k in (1, 2, max(1, len(data) // 4), max(1, len(data) // 2)):
        specs.append(("truncate", k))
    for idx in range(len(APPEND_PATTERNS)):
        specs.append(("append", idx))
    if data:
        specs.append(("duptail", 8))
    for pos in range(min(8, len(data))):
        specs.append(("flipbyte", pos))
        specs.append(("insertbyte", pos))
    return specs


# --------------------------------------------------------------------------
# spec application
# --------------------------------------------------------------------------

def apply_doc_spec(doc: Document, spec: Spec) -> Optional[Document]:
    """Apply one spec to a fresh clone; return ``None`` if inapplicable."""
    doc = clone_doc(doc)
    op = spec[0]
    idx = spec[1]

    if op == "len_pin_abs" or op == "len_pin_delta":
        node = _node_at(doc, idx)
        if node is None:
            return None
        node.pin_len = max(0, min(spec[2], 0xFFFFFFFF))
        return doc

    if op == "u32_boundary":
        node = _node_at(doc, idx)
        if node is None or node.tag != TAG_U32:
            return None
        node.value = spec[2] & 0xFFFFFFFF
        return doc

    if op == "blob_pattern":
        node = _node_at(doc, idx)
        if node is None or node.tag != TAG_BLOB:
            return None
        node.value = bytearray(BLOB_PATTERNS[spec[2]])
        return doc

    if op == "reorder":
        node = _node_at(doc, idx)
        mode = spec[2]
        if node is None or node.tag not in CONTAINER_TAGS or len(node.value) < 2:
            return None
        if mode == "swap01":
            node.value[0], node.value[1] = node.value[1], node.value[0]
        elif mode == "reverse":
            node.value.reverse()
        elif mode == "rotate":
            node.value.append(node.value.pop(0))
        return doc

    if op == "count_pin_abs":
        value = max(0, min(spec[2], 0xFFFF))
        if idx == -1:
            doc.pin_count = value
            return doc
        node = _node_at(doc, idx)
        if node is None or node.tag != TAG_STRUCT:
            return None
        node.pin_cval = value
        return doc

    if op in ("wrap", "flatten", "drop", "insert"):
        slot, pos = _locate(doc, idx)
        if slot is None:
            return None
        if op == "wrap":
            slot[pos] = Node(spec[2], [slot[pos]])
        elif op == "flatten":
            target = slot[pos]
            if target.tag not in CONTAINER_TAGS or not target.value:
                return None
            slot[pos] = target.value[0]
        elif op == "drop":
            del slot[pos]
        elif op == "insert":
            slot.insert(pos, _fresh(spec[2]))
        return doc

    raise ValueError("unknown doc op %r" % (op,))


def apply_wire_spec(data: bytes, spec: Spec) -> Optional[bytes]:
    op = spec[0]
    if op == "truncate":
        k = spec[1]
        if k <= 0 or k >= len(data):
            return None
        return data[:-k]
    if op == "append":
        return data + APPEND_PATTERNS[spec[1]]
    if op == "duptail":
        k = min(spec[1], len(data))
        return data + data[:k]
    if op == "flipbyte":
        pos = spec[1]
        if pos >= len(data):
            return None
        buf = bytearray(data)
        buf[pos] ^= 0xFF
        return bytes(buf)
    if op == "insertbyte":
        pos = spec[1]
        if pos > len(data):
            return None
        return data[:pos] + b"\x00" + data[pos:]
    raise ValueError("unknown wire op %r" % (op,))


# --------------------------------------------------------------------------
# variant generators
# --------------------------------------------------------------------------

def single_doc_variants(doc: Document) -> Iterator[Tuple[bytes, List[Spec]]]:
    for spec in enumerate_doc_specs(doc):
        mutated = apply_doc_spec(doc, spec)
        if mutated is not None:
            yield serialize_doc(mutated), [spec]


def wire_variants(doc: Document) -> Iterator[Tuple[bytes, List[Spec]]]:
    data = serialize_doc(doc)
    for spec in enumerate_wire_specs(data):
        mutated = apply_wire_spec(data, spec)
        if mutated is not None:
            yield mutated, [spec]


def pair_doc_variants(doc: Document, cap: Optional[int] = None
                      ) -> Iterator[Tuple[bytes, List[Spec]]]:
    """All 2-wise combinations of structured specs (uniformly strided cap)."""
    specs = enumerate_doc_specs(doc)
    pairs: List[Tuple[Spec, Spec]] = []
    for i, first in enumerate(specs):
        for second in specs[i:]:
            pairs.append((first, second))
    if cap is not None and len(pairs) > cap:
        stride = -(-len(pairs) // cap)
        pairs = pairs[::stride][:cap]
    for first, second in pairs:
        once = apply_doc_spec(doc, first)
        if once is None:
            continue
        twice = apply_doc_spec(once, second)
        if twice is None:
            continue
        yield serialize_doc(twice), [first, second]


def random_variants(doc: Document, rng: random.Random, count: int
                    ) -> Iterator[Tuple[bytes, List[Spec]]]:
    """Seeded random pipeline: 1-3 tree edits, then 0-2 wire edits."""
    pool = enumerate_doc_specs(doc)
    for _ in range(count):
        current = clone_doc(doc)
        applied: List[Spec] = []
        for _step in range(rng.randint(1, 3)):
            for _attempt in range(12):
                spec = rng.choice(pool)
                nxt = apply_doc_spec(current, spec)
                if nxt is not None:
                    current = nxt
                    applied.append(spec)
                    break
        data = serialize_doc(current)
        wire_pool = enumerate_wire_specs(data)
        for _step in range(rng.randint(0, 2)):
            for _attempt in range(6):
                spec = rng.choice(wire_pool)
                mutated = apply_wire_spec(data, spec)
                if mutated is not None:
                    data = mutated
                    applied.append(spec)
                    break
        yield data, applied


def sentinel_variant(doc: Document) -> Tuple[bytes, List[Spec]]:
    """One first-class U32 set to the legacy hang sentinel."""
    nodes = all_nodes(doc)
    target = next((i for i, n in enumerate(nodes) if n.tag == TAG_U32), None)
    if target is None:
        target = 0
    spec = ("u32_boundary", target, 0xDEADBEEF)
    mutated = apply_doc_spec(doc, spec)
    if mutated is None:
        mutated = apply_doc_spec(doc, ("insert", 0, TAG_U32))
        mutated = apply_doc_spec(mutated, ("u32_boundary", 0, 0xDEADBEEF))
        spec = ("insert", 0, TAG_U32)
    return serialize_doc(mutated), [spec]
