"""minipb —— 极简 TLV 二进制协议编解码库（仅标准库）。

线格式（wire format）：
    每个字段 = key + payload
    key      = varint(field_number << 3 | wire_type)
    wire_type 0 (VARINT): payload 为一个 varint
    wire_type 2 (LEN)   : payload 为 varint(length) + length 字节原始数据
                          （字符串 / 字节串 / 嵌套消息都走 LEN）

兼容规则：
    - 未知字段（未知 tag 或 wire_type 不匹配）整体跳过，原始字节原样保留，
      重新编码时原样写回，保证跨版本不丢数据。
    - 非 repeated 字段出现多次时后者覆盖前者（last wins）。
    - 长度字段先校验再读取：超出剩余数据 -> TruncatedDataError；
      超出上限 -> LimitExceededError。校验通过前绝不按声明长度分配内存。
"""

MAX_VARINT_BYTES = 10          # 单个 varint 最大字节数
DEFAULT_MAX_FIELD_LEN = 16 * 1024 * 1024   # 单个 LEN 字段默认上限 16 MiB
DEFAULT_MAX_DEPTH = 100        # 嵌套消息默认最大深度

WT_VARINT = 0
WT_LEN = 2


# ---------------------------------------------------------------- 错误类型

class ProtocolError(Exception):
    """所有协议错误的基类。"""


class TruncatedDataError(ProtocolError):
    """声明的长度超出剩余数据，或数据在 varint/字段中间被截断。"""


class LimitExceededError(ProtocolError):
    """声明的长度在剩余数据范围内，但超过配置的上限。"""


class InvalidWireTypeError(ProtocolError):
    """遇到不支持的 wire type 或非法 field number。"""


class NestingTooDeepError(ProtocolError):
    """嵌套消息深度超过上限。"""


class VarintTooLongError(ProtocolError):
    """varint 超过最大字节数仍未终止。"""


# ---------------------------------------------------------------- varint

def encode_varint(value):
    if not isinstance(value, int) or value < 0:
        raise ValueError("varint 只支持非负整数: %r" % (value,))
    out = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            return bytes(out)


def _read_varint(data, pos):
    """从 data[pos] 处读 varint，返回 (value, new_pos)。不做任何预分配。"""
    result = 0
    shift = 0
    for _ in range(MAX_VARINT_BYTES):
        if pos >= len(data):
            raise TruncatedDataError("varint 未完整：数据在偏移 %d 处结束" % pos)
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7
    raise VarintTooLongError("varint 超过 %d 字节仍未终止" % MAX_VARINT_BYTES)


# ---------------------------------------------------------------- Reader

class Reader:
    """在 bytes 上游标式读取，不做任何拷贝。"""

    __slots__ = ("data", "pos", "max_len", "max_depth", "depth")

    def __init__(self, data, *, max_len=DEFAULT_MAX_FIELD_LEN,
                 max_depth=DEFAULT_MAX_DEPTH, depth=0):
        self.data = memoryview(data)
        self.pos = 0
        self.max_len = max_len
        self.max_depth = max_depth
        self.depth = depth

    def eof(self):
        return self.pos >= len(self.data)

    def remaining(self):
        return len(self.data) - self.pos

    def read_varint(self):
        value, self.pos = _read_varint(self.data, self.pos)
        return value

    def read_key(self):
        key = self.read_varint()
        field_number, wire_type = key >> 3, key & 0x07
        if field_number == 0:
            raise InvalidWireTypeError("field number 0 非法")
        return field_number, wire_type

    def read_len_bytes(self):
        """读取 LEN 字段的 payload。

        关键：先拿到声明长度并做两项校验，校验全部通过后才触碰 payload，
        绝不按声明长度预先分配内存。
        """
        declared = self.read_varint()
        remaining = self.remaining()
        if declared > remaining:
            raise TruncatedDataError(
                "声明长度 %d 超出剩余数据 %d" % (declared, remaining))
        if declared > self.max_len:
            raise LimitExceededError(
                "声明长度 %d 超出上限 %d" % (declared, self.max_len))
        start = self.pos
        self.pos += declared
        return bytes(self.data[start:self.pos])

    def skip_payload(self, wire_type):
        """跳过未知字段的 payload（长度校验照常进行）。"""
        if wire_type == WT_VARINT:
            self.read_varint()
        elif wire_type == WT_LEN:
            self.read_len_bytes()
        else:
            raise InvalidWireTypeError("不支持的 wire type: %d" % wire_type)


# ---------------------------------------------------------------- 字段描述

_KIND_WIRE_TYPE = {"varint": WT_VARINT, "bytes": WT_LEN, "str": WT_LEN,
                   "message": WT_LEN}


class Field:
    """单个字段的 schema 描述。

    kind: 'varint' | 'bytes' | 'str' | 'message'
    """

    __slots__ = ("number", "name", "kind", "repeated", "message_cls",
                 "wire_type")

    def __init__(self, number, kind, name, *, repeated=False, message_cls=None):
        if kind not in _KIND_WIRE_TYPE:
            raise ValueError("未知字段类型: %r" % (kind,))
        if kind == "message" and message_cls is None:
            raise ValueError("message 字段必须给出 message_cls")
        self.number = number
        self.name = name
        self.kind = kind
        self.repeated = repeated
        self.message_cls = message_cls
        self.wire_type = _KIND_WIRE_TYPE[kind]

    def read_value(self, reader):
        if self.kind == "varint":
            return reader.read_varint()
        payload = reader.read_len_bytes()
        if self.kind == "bytes":
            return payload
        if self.kind == "str":
            try:
                return payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProtocolError("字段 %s 不是合法 UTF-8: %s"
                                    % (self.name, exc)) from exc
        # 嵌套消息：子 Reader 深度 +1，深度校验在 Message._read_from 入口
        sub = Reader(payload, max_len=reader.max_len,
                     max_depth=reader.max_depth, depth=reader.depth + 1)
        return self.message_cls._read_from(sub)

    def write_to(self, out, value):
        out += encode_varint(self.number << 3 | self.wire_type)
        if self.kind == "varint":
            out += encode_varint(value)
            return
        if self.kind == "str":
            payload = value.encode("utf-8")
        elif self.kind == "bytes":
            payload = bytes(value)
        else:
            payload = value.encode()
        out += encode_varint(len(payload))
        out += payload


# ---------------------------------------------------------------- 消息基类

class Message:
    """子类通过 FIELDS = {number: Field(...)} 声明 schema。"""

    FIELDS = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._by_name = {f.name: f for f in cls.FIELDS.values()}

    def __init__(self, **kwargs):
        object.__setattr__(self, "_values", {})
        object.__setattr__(self, "_unknown", [])  # 未知字段原始字节，按出现顺序
        for name, value in kwargs.items():
            setattr(self, name, value)

    # -- 属性访问：未设置的字段返回默认值（repeated -> []，其余 -> None）

    def __getattr__(self, name):
        spec = type(self)._by_name.get(name)
        if spec is None:
            raise AttributeError(name)
        if spec.repeated:
            return self._values.setdefault(name, [])
        return self._values.get(name)

    def __setattr__(self, name, value):
        if name in type(self)._by_name:
            self._values[name] = value
        else:
            object.__setattr__(self, name, value)

    def __eq__(self, other):
        return (type(self) is type(other)
                and self._values == other._values
                and self._unknown == other._unknown)

    def __repr__(self):
        return "%s(%r, unknown=%d)" % (type(self).__name__, self._values,
                                       len(self._unknown))

    @property
    def unknown_fields(self):
        """未知字段的原始字节列表（含 key），只读视图。"""
        return tuple(self._unknown)

    # -- 解码

    @classmethod
    def decode(cls, data, *, max_len=DEFAULT_MAX_FIELD_LEN,
               max_depth=DEFAULT_MAX_DEPTH):
        reader = Reader(data, max_len=max_len, max_depth=max_depth)
        return cls._read_from(reader)

    @classmethod
    def _read_from(cls, reader):
        if reader.depth > reader.max_depth:
            raise NestingTooDeepError(
                "嵌套深度 %d 超出上限 %d" % (reader.depth, reader.max_depth))
        msg = cls()
        while not reader.eof():
            start = reader.pos
            field_number, wire_type = reader.read_key()
            spec = cls.FIELDS.get(field_number)
            if spec is not None and spec.wire_type == wire_type:
                value = spec.read_value(reader)
                if spec.repeated:
                    msg._values.setdefault(spec.name, []).append(value)
                else:
                    msg._values[spec.name] = value  # last wins
            else:
                # 未知 tag 或 wire type 不匹配：跳过并保留原始字节
                reader.skip_payload(wire_type)
                msg._unknown.append(bytes(reader.data[start:reader.pos]))
        return msg

    # -- 编码

    def encode(self):
        out = bytearray()
        for number in sorted(type(self).FIELDS):
            spec = type(self).FIELDS[number]
            if spec.name not in self._values:
                continue
            value = self._values[spec.name]
            if spec.repeated:
                for item in value:
                    spec.write_to(out, item)
            else:
                spec.write_to(out, value)
        for raw in self._unknown:      # 未知字段原样写回，不丢失
            out += raw
        return bytes(out)
