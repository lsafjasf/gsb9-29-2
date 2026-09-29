"""结构感知变异器。

变异器分两类：
  - 结构变异器：作用于 Message 字段树（is_post=False）
  - 后序列化变异器：作用于序列化字节（is_post=True），如长度字段篡改、字节翻转

所有变异器只依赖注入的 random.Random 实例，保证种子可复现。
"""

import random

from message import (
    Field,
    Message,
    TAG_CONTAINER,
    TAG_INT,
    TAG_RAW,
    TAG_STR,
)

MAX_DEPTH = 1500  # 嵌套变异允许的最大的深度（序列化器上限 2000）


def _all_entries(fields, out=None):
    """迭代收集 (parent_list, index, field)，含所有嵌套层。"""
    if out is None:
        out = []
    stack = [fields]
    while stack:
        current = stack.pop()
        for index, field in enumerate(current):
            out.append((current, index, field))
            if field.tag == TAG_CONTAINER:
                stack.append(field.value)
    return out


def _wrap(field, k):
    for _ in range(k):
        field = Field(TAG_CONTAINER, [field])
    return field


class Mutator:
    name = "base"
    is_post = False

    def mutate(self, msg, rng):  # pragma: no cover - 抽象方法
        raise NotImplementedError

    def mutate_bytes(self, data, offsets, rng):  # pragma: no cover
        raise NotImplementedError


class LengthTamperMutator(Mutator):
    """长度字段篡改：替换为边界值/邻近值/随机值（后序列化，结构感知）。"""

    name = "length_tamper"
    is_post = True

    def mutate_bytes(self, data, offsets, rng):
        if not offsets:
            return data
        off = rng.choice(offsets)
        actual = int.from_bytes(data[off:off + 2], "big")
        candidates = [
            0, 1, 2,
            max(0, actual - 1),
            min(0xFFFF, actual + 1),
            0x7FFF, 0x8000, 0xFFFF,
            actual ^ 0xFFFF,
            rng.randrange(0x10000),
        ]
        new_val = rng.choice(candidates)
        buf = bytearray(data)
        buf[off:off + 2] = new_val.to_bytes(2, "big")
        return bytes(buf)


class NestingMutator(Mutator):
    """嵌套层级增删：包裹（增层）、解包（降层）、删除子树。"""

    name = "nesting"

    def mutate(self, msg, rng):
        op = rng.choice(["wrap", "wrap", "unwrap", "delete"])
        if op == "wrap":
            k = rng.choice([1, 1, 2, 4, 8, 16, 32, 64, 128, 256, 512])
            k = min(k, MAX_DEPTH - msg.depth())
            if k <= 0:
                return
            if not msg.fields or rng.random() < 0.4:
                msg.fields = [Field(TAG_CONTAINER, msg.fields)]
                for _ in range(k - 1):
                    msg.fields = [Field(TAG_CONTAINER, msg.fields)]
            else:
                entries = _all_entries(msg.fields)
                parent, index, field = rng.choice(entries)
                parent[index] = _wrap(field, k)
        elif op == "unwrap":
            containers = [e for e in _all_entries(msg.fields)
                          if e[2].tag == TAG_CONTAINER]
            if containers:
                parent, index, field = rng.choice(containers)
                parent[index:index + 1] = field.value
        else:  # delete
            entries = _all_entries(msg.fields)
            if entries:
                parent, index, _ = rng.choice(entries)
                del parent[index]


class BoundaryValueMutator(Mutator):
    """边界值替换：整数/字符串/RAW 载荷替换为典型边界值。"""

    name = "boundary_value"

    INT_BOUNDARIES = [0, 1, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFE, 0xFFFFFFFF]
    STR_BOUNDARIES = [b"", b"A", b"A" * 255, b"\x00" * 16, b"\xff" * 64,
                      b"A" * 4096]
    RAW_LENS = [0, 1, 255, 4096, 0xFFF0]

    def mutate(self, msg, rng):
        candidates = [e for e in _all_entries(msg.fields)
                      if e[2].tag in (TAG_INT, TAG_STR, TAG_RAW)]
        if not candidates:
            return
        _, _, field = rng.choice(candidates)
        if field.tag == TAG_INT:
            field.value = rng.choice(self.INT_BOUNDARIES)
        elif field.tag == TAG_STR:
            field.value = rng.choice(self.STR_BOUNDARIES)
        else:
            n = rng.choice(self.RAW_LENS)
            field.value = bytes([rng.choice([0x00, 0x41, 0xAB, 0xFF])]) * n


class FieldReorderMutator(Mutator):
    """字段重排：打乱/反转/交换兄弟字段，或复制字段。"""

    name = "field_reorder"

    def mutate(self, msg, rng):
        groups = []
        stack = [msg.fields]
        while stack:
            group = stack.pop()
            if group:
                groups.append(group)
            for field in group:
                if field.tag == TAG_CONTAINER:
                    stack.append(field.value)
        if not groups:
            return
        group = rng.choice(groups)
        op = rng.choice(["shuffle", "reverse", "swap", "duplicate"])
        if op == "shuffle" and len(group) >= 2:
            rng.shuffle(group)
        elif op == "reverse" and len(group) >= 2:
            group.reverse()
        elif op == "swap" and len(group) >= 2:
            i, j = rng.randrange(len(group)), rng.randrange(len(group))
            group[i], group[j] = group[j], group[i]
        else:  # duplicate
            index = rng.randrange(len(group))
            group.insert(index, group[index].clone())


class ByteFlipMutator(Mutator):
    """字节翻转基线（非结构感知，用于对照实验）。"""

    name = "byte_flip"
    is_post = True

    def mutate_bytes(self, data, offsets, rng):
        if not data:
            return data
        buf = bytearray(data)
        for _ in range(rng.randint(1, 8)):
            i = rng.randrange(len(buf))
            buf[i] ^= 1 << rng.randrange(8)
        return bytes(buf)


STRUCT_MUTATORS = [
    LengthTamperMutator(),
    NestingMutator(),
    BoundaryValueMutator(),
    FieldReorderMutator(),
]

RANDOM_MUTATORS = [ByteFlipMutator()]
