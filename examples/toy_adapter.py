"""示例适配器：玩具 TLV 协议 + 两个被故意植入差异的解析实现。

报文格式
--------
    报文   = MAGIC(0xAB) 记录*
    记录   = 类型(1B) 长度(1B) 载荷(长度B)
    uint   = 类型 0x01，载荷为大端无符号整数
    str    = 类型 0x02，载荷为原始字节
    group  = 类型 0x03，载荷为嵌套的 记录*

两个实现的故意差异（用于演示框架的分类与归约能力）
---------------------------------------------------
    实现 A（参照）：uint 长度只允许 1/2/4/8；允许空 str；最大深度 8
    实现 B（新写）：uint 长度允许 1..8；空 str 报 EmptyString；最大深度 4
    两者共同限制：整报文 > 4096 字节报 TooLarge
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from difftest.spec import ProtocolSpec

MAGIC = 0xAB
T_UINT, T_STR, T_GROUP = 0x01, 0x02, 0x03
MAX_SIZE = 4096


class ToyError(Exception):
    """所有协议错误的基类。"""


class BadMagic(ToyError):
    pass


class Truncated(ToyError):
    pass


class BadType(ToyError):
    pass


class BadUintSize(ToyError):
    pass


class EmptyString(ToyError):
    pass


class DepthExceeded(ToyError):
    pass


class TooLarge(ToyError):
    pass


def make_parser(*, max_depth, uint_sizes, allow_empty_str):
    def parse(data):
        if len(data) > MAX_SIZE:
            raise TooLarge(f"message size {len(data)} > {MAX_SIZE}")
        if not data or data[0] != MAGIC:
            raise BadMagic("missing or bad magic byte")

        def parse_fields(buf, depth):
            if depth > max_depth:
                raise DepthExceeded(f"depth {depth} > {max_depth}")
            out = []
            pos = 0
            while pos < len(buf):
                if pos + 2 > len(buf):
                    raise Truncated("record header truncated")
                rtype, rlen = buf[pos], buf[pos + 1]
                pos += 2
                if pos + rlen > len(buf):
                    raise Truncated("record payload truncated")
                payload = buf[pos : pos + rlen]
                pos += rlen
                if rtype == T_UINT:
                    if rlen not in uint_sizes:
                        raise BadUintSize(f"uint size {rlen}")
                    out.append(["uint", int.from_bytes(payload, "big")])
                elif rtype == T_STR:
                    if rlen == 0 and not allow_empty_str:
                        raise EmptyString("str length 0")
                    out.append(["str", payload.hex()])
                elif rtype == T_GROUP:
                    out.append(["group", parse_fields(payload, depth + 1)])
                else:
                    raise BadType(f"unknown type 0x{rtype:02x}")
            return out

        return parse_fields(data[1:], 1)

    return parse


parse_a = make_parser(max_depth=8, uint_sizes={1, 2, 4, 8}, allow_empty_str=True)
parse_b = make_parser(max_depth=4, uint_sizes=set(range(1, 9)), allow_empty_str=False)


class ToySpec(ProtocolSpec):
    name = "toy"

    # ------------------------------------------------------------------
    # 结构化生成
    # ------------------------------------------------------------------
    def random_message(self, rng, cfg):
        fields = [self._field(rng, 1, cfg.gen_max_depth) for _ in range(rng.randint(0, 4))]
        return fields, {}

    def _field(self, rng, depth, max_depth):
        kinds = ["uint", "str"] + (["group"] if depth < max_depth else [])
        kind = rng.choice(kinds)
        if kind == "uint":
            # 多数合法，少量落在尺寸边界/非法值上
            size = rng.choice([1, 2, 4, 8, 1, 2, 4, 8, 0, 3, 9])
            value = rng.getrandbits(8 * size) if 0 < size <= 8 else 0
            return ("uint", size, value)
        if kind == "str":
            n = rng.choice([0, 1, 2, 8, 32, 254, 255, rng.randint(0, 255)])
            return ("str", rng.randbytes(n))
        children = [self._field(rng, depth + 1, max_depth) for _ in range(rng.randint(0, 3))]
        return ("group", children)

    def boundary_message(self, rng, cfg):
        fields = []
        for n in (0, 1, 254, 255):
            fields.append(("str", rng.randbytes(n)))
        for size in (0, 1, 8, 9):
            value = rng.getrandbits(8 * size) if 0 < size <= 8 else 0
            fields.append(("uint", size, value))
        fields.append(("group", []))
        rng.shuffle(fields)
        return fields[: rng.randint(1, len(fields))], {}

    def nesting_message(self, rng, cfg):
        depth = rng.randint(1, cfg.nesting_depth)
        if rng.random() < 0.5:
            leaf = ("uint", rng.choice([1, 3, 9]), 7)
        else:
            leaf = ("str", rng.randbytes(rng.choice([0, 4])))
        node = leaf
        for _ in range(depth - 1):
            node = ("group", [node])
        return [node], {"depth": depth}

    def mutate_tree(self, tree, rng, cfg):
        tree = list(tree)
        op = rng.choice(["add", "remove", "wrap", "dup"])
        if op == "add" or not tree:
            tree.insert(rng.randint(0, len(tree)), self._field(rng, 1, cfg.gen_max_depth))
        elif op == "remove":
            tree.pop(rng.randrange(len(tree)))
        elif op == "wrap":  # 改变嵌套层级
            i = rng.randrange(len(tree))
            tree[i] = ("group", [tree[i]])
        else:  # dup
            i = rng.randrange(len(tree))
            tree.insert(i, tree[i])
        return tree, {"mutation": op}

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------
    def serialize(self, tree):
        return bytes([MAGIC]) + self._serialize_fields(tree)

    def _serialize_fields(self, fields):
        out = bytearray()
        for f in fields:
            if f[0] == "uint":
                size, value = f[1], f[2]
                payload = value.to_bytes(size, "big") if size > 0 else b""
                out += bytes([T_UINT, size & 0xFF]) + payload
            elif f[0] == "str":
                out += bytes([T_STR, len(f[1]) & 0xFF]) + f[1][:255]
            else:
                children = list(f[1])
                while True:  # group 载荷长度受 1 字节限制，超长则丢弃末尾子记录
                    payload = self._serialize_fields(children)
                    if len(payload) <= 255 or not children:
                        break
                    children = children[:-1]
                out += bytes([T_GROUP, len(payload)]) + payload
        return bytes(out)

    # ------------------------------------------------------------------
    # 极端情形与统计
    # ------------------------------------------------------------------
    def edge_case(self, kind, rng, cfg):
        if kind == "empty":
            return b""
        if kind == "oversized":
            fields = []
            total = 1
            while total <= cfg.oversize_bytes:
                fields.append(("str", rng.randbytes(255)))
                total += 257
            return self.serialize(fields)
        if kind == "deep":
            depth = min(cfg.deep_depth, 126)  # 受单字节长度字段约束的最大深度
            node = ("uint", 1, 0)
            for _ in range(depth - 1):
                node = ("group", [node])
            return self.serialize([node])
        if kind == "all_illegal":
            data = bytearray(rng.randbytes(rng.randint(1, 64)))
            if data and data[0] == MAGIC:
                data[0] ^= 0xFF
            return bytes(data)
        return None

    def stats(self, tree):
        def walk(fields, depth):
            best = depth
            for f in fields:
                if f[0] == "group":
                    best = max(best, walk(f[1], depth + 1))
            return best

        return {"depth": walk(tree, 0) if tree else 0}


SPEC = ToySpec()
