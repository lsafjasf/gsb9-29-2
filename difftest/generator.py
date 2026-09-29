"""结构化随机报文生成器。

每个用例附带 tags（命中的特性集合），供覆盖率统计使用。
前若干个用例固定覆盖必备边界（空报文 / 超大报文 / 深度极大 / 全部非法），
其余用例按权重随机选择策略生成：
  - 合法嵌套树（随机深度/扇出，含长度边界载荷）
  - 长度边界（0/1/255/256/65535，UINT 0..9 字节）
  - 截断（字段缺失）
  - 字节变异（字段增删、类型字节篡改）
  - 纯垃圾（全部非法序列）
"""

import random

T_NODE, T_STR, T_UINT = 0x01, 0x02, 0x03

BOUNDARY_SIZES = (0, 1, 2, 254, 255, 256, 257, 65534, 65535)
UINT_SIZES = (0, 1, 2, 4, 7, 8, 9)


def _record(rtype, payload):
    return bytes((rtype, (len(payload) >> 8) & 0xFF, len(payload) & 0xFF)) + payload


class Generator:
    def __init__(self, seed=1, max_depth=32, oversized_threshold=70000,
                 max_fanout=6):
        self.rng = random.Random(seed)
        self.max_depth = max_depth
        self.oversized_threshold = oversized_threshold
        self.max_fanout = max_fanout
        self._strategies = [
            (30, self._valid_tree),
            (15, self._boundary_lengths),
            (15, self._truncated),
            (20, self._mutated),
            (10, self._garbage),
            (5, self._deep_chain),
            (5, self._invalid_utf8),
        ]

    # ---- 对外接口 -------------------------------------------------------

    def cases(self, n):
        """生成 n 个 (data: bytes, tags: frozenset) 用例。"""
        fixed = [
            (b"", {"empty"}),
            (self._make_oversized(), {"oversized", "valid"}),
            (self._make_chain(self.max_depth), {"deep_nesting", "valid"}),
            (self._make_chain(self.max_depth + 4), {"deep_nesting"}),
            (self._make_garbage(), {"all_illegal"}),
        ]
        for data, tags in fixed[:n]:
            yield data, frozenset(tags)
        for _ in range(max(0, n - len(fixed))):
            yield self._random_case()

    # ---- 随机策略 -------------------------------------------------------

    def _random_case(self):
        total = sum(w for w, _ in self._strategies)
        pick = self.rng.uniform(0, total)
        for weight, fn in self._strategies:
            pick -= weight
            if pick <= 0:
                return fn()
        return self._valid_tree()

    MAX_TREE_RECORDS = 200  # 单棵随机树的记录数预算，防止指数爆炸

    def _valid_tree(self):
        if self.rng.random() < 0.15:
            # 小概率逼近深度上限，覆盖深度边界
            depth_limit = self.rng.randint(max(1, self.max_depth - 2),
                                           self.max_depth + 2)
        else:
            depth_limit = self.rng.randint(1, 6)
        self._budget = self.MAX_TREE_RECORDS
        data, used_boundary, actual_depth = self._gen_seq(depth_limit, 1)
        tags = {"valid"}
        if used_boundary:
            tags.add("boundary_length")
        if actual_depth >= self.max_depth - 1:
            tags.add("deep_nesting")
        return data, frozenset(tags)

    def _gen_seq(self, depth_limit, depth):
        """生成一个记录序列，返回 (bytes, 是否用到边界长度, 实际最大深度)。"""
        count = self.rng.randint(0, min(self.max_fanout, self._budget))
        parts, used_boundary, max_seen = [], False, depth
        for _ in range(count):
            rec, b, d = self._gen_record(depth_limit, depth)
            parts.append(rec)
            used_boundary = used_boundary or b
            max_seen = max(max_seen, d)
        return b"".join(parts), used_boundary, max_seen

    def _gen_record(self, depth_limit, depth):
        self._budget -= 1
        choices = ["str", "uint"]
        if depth < depth_limit and self._budget > 0:
            choices += ["node", "node"]
        kind = self.rng.choice(choices)
        if kind == "node":
            payload, used_boundary, max_seen = self._gen_seq(depth_limit,
                                                             depth + 1)
            return _record(T_NODE, payload), used_boundary, max_seen
        if kind == "uint":
            size = self.rng.choice((1, 2, 4, 8))
            payload = self.rng.randbytes(size)
            return _record(T_UINT, payload), size in BOUNDARY_SIZES, depth
        size = self.rng.randint(0, 32)
        used_boundary = False
        if self.rng.random() < 0.2:
            size = self.rng.choice(BOUNDARY_SIZES)
            used_boundary = True
        payload = self._rand_text(size)
        return _record(T_STR, payload), used_boundary, depth

    def _rand_text(self, size):
        # 生成合法 UTF-8，长度按字节数近似控制
        out = bytearray()
        while len(out) < size:
            ch = self.rng.choice("abcXYZ018 \t中文字符")
            out += ch.encode("utf-8")
        return bytes(out[:size])

    def _boundary_lengths(self):
        tags = {"boundary_length"}
        kind = self.rng.choice(("str", "uint"))
        if kind == "uint":
            size = self.rng.choice(UINT_SIZES)
            tags.add("uint_boundary")
            return _record(T_UINT, self.rng.randbytes(size)), frozenset(tags)
        size = self.rng.choice(BOUNDARY_SIZES)
        return _record(T_STR, self._rand_text(size)), frozenset(tags)

    def _truncated(self):
        data, _ = self._valid_tree()
        if not data:
            data = _record(T_STR, b"abcdef")
        cut = self.rng.randint(0, len(data) - 1)
        return data[:cut], frozenset({"truncated"})

    def _mutated(self):
        data, _ = self._valid_tree()
        if not data:
            data = _record(T_UINT, b"\x01")
        buf = bytearray(data)
        tags = {"mutation"}
        for _ in range(self.rng.randint(1, 4)):
            op = self.rng.choice(("flip", "insert", "delete", "retipe"))
            if op == "flip" and buf:
                i = self.rng.randrange(len(buf))
                buf[i] ^= 1 << self.rng.randint(0, 7)
            elif op == "insert":
                i = self.rng.randint(0, len(buf))
                buf[i:i] = self.rng.randbytes(self.rng.randint(1, 4))
                tags.add("field_added")
            elif op == "delete" and buf:
                i = self.rng.randrange(len(buf))
                del buf[i:i + self.rng.randint(1, min(4, len(buf) - i))]
                tags.add("field_removed")
            elif op == "retipe" and len(buf) >= 1:
                i = self.rng.randrange(len(buf))
                buf[i] = self.rng.choice((0x00, 0x7F, 0xFF, T_NODE, T_STR))
                tags.add("unknown_type")
        return bytes(buf), frozenset(tags)

    def _garbage(self):
        return self._make_garbage(), frozenset({"all_illegal"})

    def _invalid_utf8(self):
        payload = bytes((0xE4, 0xB8, 0xAD, 0xFF, 0xFE))  # 合法前缀 + 非法字节
        return _record(T_STR, payload), frozenset({"invalid_utf8"})

    def _deep_chain(self):
        depth = self.rng.randint(max(1, self.max_depth - 2), self.max_depth + 4)
        tags = {"deep_nesting"}
        if depth > self.max_depth:
            tags.add("deep_over_limit")
        return self._make_chain(depth), frozenset(tags)

    # ---- 固定边界用例的构造 ---------------------------------------------

    def _make_chain(self, depth):
        """depth 层嵌套 NODE，最内层放一个空 STR。"""
        payload = _record(T_STR, b"")
        for _ in range(depth):
            payload = _record(T_NODE, payload)
        return payload

    def _make_oversized(self):
        parts, total = [], 0
        while total <= self.oversized_threshold:
            rec = _record(T_STR, self._rand_text(self.rng.randint(100, 500)))
            parts.append(rec)
            total += len(rec)
        return b"".join(parts)

    def _make_garbage(self):
        return self.rng.randbytes(self.rng.randint(1, 64))
