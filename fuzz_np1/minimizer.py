"""Failure-preserving input minimisation.

The oracle is the runner: a candidate still "fails" if it produces the same
bug class (or still times out).  Two passes run in sequence:

1. structured shrink on the typed tree (drop children, truncate blobs) when
   the failing input parses as NP1;
2. classic Zeller-style ``ddmin`` chunk elimination on raw bytes.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from .protocol import (
    Document,
    Node,
    TAG_BLOB,
    TAG_LIST,
    TAG_STRUCT,
    TAG_U32,
    clone_doc,
    serialize_doc,
)
from .sut import parse_message

Oracle = Callable[[bytes], bool]


def _try_parse_doc(data: bytes) -> Optional[Document]:
    try:
        nodes = parse_message(data)
    except Exception:
        return None
    return _nodes_to_doc(nodes)


def _nodes_to_doc(parsed) -> Document:
    def conv(item) -> Node:
        tag, value = item
        if tag == "u32":
            return Node(TAG_U32, value)
        if tag == "blob":
            return Node(TAG_BLOB, bytearray(value))
        if tag == "list":
            return Node(TAG_LIST, [conv(c) for c in value])
        if tag == "struct":
            return Node(TAG_STRUCT, [conv(c) for c in value])
        raise ValueError(tag)

    return Document(0, [conv(n) for n in parsed])


def _structured_candidates(data: bytes) -> List[bytes]:
    doc = _try_parse_doc(data)
    if doc is None:
        return []
    out: List[bytes] = []
    nodes: List[Tuple[Node, list, int]] = []

    def walk(items: list) -> None:
        for i, node in enumerate(items):
            nodes.append((node, items, i))
            if node.tag in (TAG_LIST, TAG_STRUCT):
                walk(node.value)

    walk(doc.children)
    for node, items, i in nodes:
        trial = clone_doc(doc)
        # locate the twin of (items, i) inside the clone by pre-order index
        idx = _preorder_index(doc, node)
        if idx is None:
            continue
        twin_parent, twin_pos = _slot_at(trial, idx)
        if twin_parent is None:
            continue
        del twin_parent[twin_pos]
        out.append(serialize_doc(trial))
        if node.tag == TAG_BLOB and len(node.value) > 1:
            trial2 = clone_doc(doc)
            parent2, pos2 = _slot_at(trial2, idx)
            parent2[pos2].value = bytearray(node.value[: max(1, len(node.value) // 2)])
            out.append(serialize_doc(trial2))
    return out


def _preorder_index(doc: Document, target: Node) -> Optional[int]:
    counter = [0]
    found = [None]

    def walk(node: Node) -> bool:
        if node is target:
            found[0] = counter[0]
            return True
        counter[0] += 1
        if node.tag in (TAG_LIST, TAG_STRUCT):
            for child in node.value:
                if walk(child):
                    return True
        return False

    for child in doc.children:
        if walk(child):
            break
    return found[0]


def _slot_at(doc: Document, index: int):
    counter = [0]
    slot = [None]

    def walk(items: list) -> bool:
        for i, node in enumerate(items):
            if counter[0] == index:
                slot[0] = (items, i)
                return True
            counter[0] += 1
            if node.tag in (TAG_LIST, TAG_STRUCT):
                if walk(node.value):
                    return True
        return False

    walk(doc.children)
    return slot[0] if slot[0] else (None, None)


def ddmin(data: bytes, oracle: Oracle, max_tries: int = 400) -> bytes:
    """Chunk-elimination minimiser; keeps only failing candidates."""
    if len(data) < 2:
        return data
    tries = 0
    n = 2
    current = data
    while len(current) >= 2 and tries < max_tries:
        chunk = max(1, len(current) // n)
        reduced = False
        for start in range(0, len(current), chunk):
            if tries >= max_tries:
                break
            tries += 1
            candidate = current[:start] + current[start + chunk:]
            if candidate and oracle(candidate):
                current = candidate
                n = max(2, n - 1)
                reduced = True
                break
        if not reduced:
            if n >= len(current):
                break
            n = min(len(current), n * 2)
    return current


def minimize(data: bytes, oracle: Oracle, max_tries: int = 400,
             structured: bool = True) -> Tuple[bytes, int]:
    """Return (minimised bytes, oracle invocations).

    ``structured=False`` skips the in-process tree pass (mandatory for
    timeout findings, whose parse would hang this process).
    """
    calls = [0]

    def counted(candidate: bytes) -> bool:
        calls[0] += 1
        return oracle(candidate)

    best = data
    if structured:
        for candidate in _structured_candidates(best):
            if calls[0] >= max_tries:
                break
            if 0 < len(candidate) < len(best) and counted(candidate):
                best = candidate
    best = ddmin(best, counted, max_tries=max_tries - calls[0])
    return best, calls[0]
