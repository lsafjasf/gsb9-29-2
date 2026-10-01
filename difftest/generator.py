"""结构化随机用例生成。

前 4 个用例固定为四类极端情形（保证覆盖），
之后按策略随机生成：随机树 / 长度边界 / 字段增删 / 嵌套变化 / 非法序列。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

EDGE_KINDS = ("empty", "oversized", "deep", "all_illegal")
EDGE_LABELS = {
    "empty": "空报文",
    "oversized": "超大报文",
    "deep": "深度极大",
    "all_illegal": "全部非法",
}

STRATEGIES = (
    "random_tree",
    "length_boundary",
    "field_mutation",
    "nesting",
    "illegal_sequence",
)


@dataclass
class Case:
    data: bytes | None
    tag: str
    meta: dict = field(default_factory=dict)


class Generator:
    def __init__(self, spec, cfg, seed):
        self.spec = spec
        self.cfg = cfg
        self.rng = random.Random(seed)

    def cases(self, total):
        for i in range(total):
            if i < len(EDGE_KINDS):
                kind = EDGE_KINDS[i]
                data = self.spec.edge_case(kind, self.rng, self.cfg)
                meta = {"edge": kind}
                if data is not None:
                    meta["size"] = len(data)
                yield Case(data, f"edge:{kind}", meta)
            else:
                yield self._structured(self.rng.choice(STRATEGIES))

    def _structured(self, strategy):
        spec, rng, cfg = self.spec, self.rng, self.cfg
        if strategy == "random_tree":
            tree, meta = spec.random_message(rng, cfg)
            data = spec.serialize(tree)
        elif strategy == "length_boundary":
            tree, meta = spec.boundary_message(rng, cfg)
            data = spec.serialize(tree)
        elif strategy == "field_mutation":
            tree, meta = spec.random_message(rng, cfg)
            tree, extra = spec.mutate_tree(tree, rng, cfg)
            meta = {**meta, **extra}
            data = spec.serialize(tree)
        elif strategy == "nesting":
            tree, meta = spec.nesting_message(rng, cfg)
            data = spec.serialize(tree)
        else:  # illegal_sequence
            tree, meta = spec.random_message(rng, cfg)
            base = spec.serialize(tree)
            tag, data = rng.choice(spec.byte_mutations(base, rng))
            meta = {**meta, "byte_mutation": tag}
        meta = dict(meta)
        meta.update(spec.stats(tree))
        meta["size"] = len(data)
        return Case(data, strategy, meta)
