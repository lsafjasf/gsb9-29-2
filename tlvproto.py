"""tlvproto —— 跨版本兼容的 TLV 二进制协议编解码库（仅标准库）。

线格式（wire format）：
    每个字段 = tag(varint) + payload
    tag = (field_number << 3) | wire_type
    wire_type:
        0  VARINT   1..10 字节变长整数 (uint64)
        1  FIXED64  8 字节小端
        2  LEN      varint 长度 + 长度个字节 (bytes / string / 嵌套消息)
        5  FIXED32  4 字节小端

兼容规则：
    * 解码时遇到 schema 中不存在的字段号，按 wire_type 跳过，
      原始字节（含 tag）被完整保留在 Message.unknown 中，
      重新编码时原样写回，不丢不错。
    * 新增字段对旧版透明；旧版数据缺少新字段时新字段缺席（由上层给默认值）。

长度校验（先于任何分配/拷贝）：
    * 声明长度 > max_length      -> LimitExceededError
    * 声明长度 > 剩余数据        -> TruncatedError
    两者是可区分的不同异常类型。
"""

from dataclasses import dataclass
import struct

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN = 2
WIRE_FIXED32 = 5

DEFAULT_MAX_LENGTH = 16 * 1024 * 1024  # 单个长度字段的上限
DEFAULT_MAX_DEPTH = 100                # 嵌套消息深度上限（远小于 Python 递归上限）
_MAX_VARINT_BYTES = 10                 # uint64 varint 最多 10 字节


# ---------------------------------------------------------------- 错误类型

class ProtocolError(Exception):
    """编解码错误基类。"""


class TruncatedError(ProtocolError):
    """数据被截断：声明长度超出剩余数据，或字段在缓冲区末尾前未结束。"""


class LimitExceededError(ProtocolError):
    """声明长度超过配置上限（与截断是可区分的不同错误）。"""


class DepthLimitError(ProtocolError):
    """嵌套深度超过上限。"""


class UnknownWireTypeError(ProtocolError):
    """遇到非法 / 不支持的 wire type。"""


class SchemaError(ProtocolError):
    """已知字段的实际 wire type 与 schema 声明不符。"""


# ---------------------------------------------------------------- schema

_KIND_WIRE = {
    'varint': WIRE_VARINT,
    'fixed32': WIRE_FIXED32,
    'fixed64': WIRE_FIXED64,
    'bytes': WIRE_LEN,
    'string': WIRE_LEN,
    'message': WIRE_LEN,
}


@dataclass(frozen=True)
class Field:
    """消息字段描述。

    number:   字段号（>=1，线上 tag 的一部分）
    name:     解码后 dict 中的键名
    kind:     'varint' | 'fixed32' | 'fixed64' | 'bytes' | 'string' | 'message'
    repeated: True 时值是列表，允许同号字段出现多次
    schema:   kind == 'message' 时子消息的 Field 元组/列表
    """
    number: int
    name: str
    kind: str
    repeated: bool = False
    schema: tuple = ()

    def __post_init__(self):
        if self.number < 1:
            raise ValueError('字段号必须 >= 1')
        if self.kind not in _KIND_WIRE:
            raise ValueError(f'未知字段类型: {self.kind!r}')
        if self.kind == 'message' and not isinstance(self.schema, (tuple, list)):
            raise ValueError('message 字段必须携带子 schema')
        if self.kind != 'message' and self.schema:
            raise ValueError('非 message 字段不应携带子 schema')


class Message:
    """解码结果：fields 为已识别字段，unknown 为未知字段原始字节列表。"""

    __slots__ = ('fields', 'unknown')

    def __init__(self, fields=None, unknown=None):
        self.fields = dict(fields) if fields else {}
        self.unknown = list(unknown) if unknown else []

    def __eq__(self, other):
        return (isinstance(other, Message)
                and self.fields == other.fields
                and self.unknown == other.unknown)

    def __repr__(self):
        return f'Message(fields={self.fields!r}, unknown={self.unknown!r})'


# ---------------------------------------------------------------- varint

def encode_varint(value):
    if not isinstance(value, int) or not 0 <= value < 1 << 64:
        raise ValueError(f'varint 只支持 uint64，收到: {value!r}')
    out = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            return bytes(out)


def _read_varint(data, pos, end):
    result = 0
    shift = 0
    for _ in range(_MAX_VARINT_BYTES):
        if pos >= end:
            raise TruncatedError('varint 在缓冲区末尾前未结束')
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            if result >= 1 << 64:
                raise ProtocolError('varint 超出 uint64 范围')
            return result, pos
        shift += 7
    raise ProtocolError('varint 超过 10 字节')


# ---------------------------------------------------------------- 长度校验

def _read_len_delimited(data, pos, end, max_length):
    """读取 LEN 型字段的负载。先校验，后切片，绝不按声明长度预先分配。"""
    length, pos = _read_varint(data, pos, end)
    if length > max_length:
        raise LimitExceededError(
            f'声明长度 {length} 超过上限 {max_length}')
    remaining = end - pos
    if length > remaining:
        raise TruncatedError(
            f'声明长度 {length} 超出剩余数据 {remaining} 字节')
    return data[pos:pos + length], pos + length


# ---------------------------------------------------------------- 解码

def decode(schema, data, *, max_length=DEFAULT_MAX_LENGTH,
           max_depth=DEFAULT_MAX_DEPTH):
    """按 schema 解码 data，返回 Message。未知字段原样保留在 Message.unknown。"""
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    by_tag = {f.number: f for f in schema}
    return _decode_message(by_tag, data, 0, len(data), 0, max_depth, max_length)


def _decode_message(by_tag, data, pos, end, depth, max_depth, max_length):
    if depth > max_depth:
        raise DepthLimitError(f'嵌套深度超过上限 {max_depth}')
    fields = {}
    unknown = []
    while pos < end:
        field_start = pos
        tag, pos = _read_varint(data, pos, end)
        wire = tag & 0x07
        number = tag >> 3
        if number == 0:
            raise ProtocolError('字段号 0 非法')
        fld = by_tag.get(number)
        if fld is None:
            pos = _skip_value(data, pos, end, wire, max_length)
            unknown.append(data[field_start:pos])
            continue
        if wire != _KIND_WIRE[fld.kind]:
            raise SchemaError(
                f'字段 {fld.name}(#{number}) 声明为 {fld.kind}'
                f'(wire {_KIND_WIRE[fld.kind]})，实际 wire type 为 {wire}')
        value, pos = _read_typed(fld, data, pos, end, depth, max_depth,
                                 max_length)
        if fld.repeated:
            fields.setdefault(fld.name, []).append(value)
        else:
            fields[fld.name] = value
    return Message(fields, unknown)


def _skip_value(data, pos, end, wire, max_length):
    """按 wire type 跳过未知字段，返回新的 pos。"""
    if wire == WIRE_VARINT:
        _, pos = _read_varint(data, pos, end)
        return pos
    if wire == WIRE_FIXED64:
        if end - pos < 8:
            raise TruncatedError('fixed64 字段数据不足 8 字节')
        return pos + 8
    if wire == WIRE_LEN:
        _, pos = _read_len_delimited(data, pos, end, max_length)
        return pos
    if wire == WIRE_FIXED32:
        if end - pos < 4:
            raise TruncatedError('fixed32 字段数据不足 4 字节')
        return pos + 4
    raise UnknownWireTypeError(f'不支持的 wire type: {wire}')


def _read_typed(fld, data, pos, end, depth, max_depth, max_length):
    kind = fld.kind
    if kind == 'varint':
        return _read_varint(data, pos, end)
    if kind == 'fixed32':
        if end - pos < 4:
            raise TruncatedError('fixed32 字段数据不足 4 字节')
        return struct.unpack_from('<I', data, pos)[0], pos + 4
    if kind == 'fixed64':
        if end - pos < 8:
            raise TruncatedError('fixed64 字段数据不足 8 字节')
        return struct.unpack_from('<Q', data, pos)[0], pos + 8
    payload, pos = _read_len_delimited(data, pos, end, max_length)
    if kind == 'bytes':
        return payload, pos
    if kind == 'string':
        try:
            return payload.decode('utf-8'), pos
        except UnicodeDecodeError as exc:
            raise ProtocolError(f'字段 {fld.name} UTF-8 解码失败: {exc}')
    # 嵌套消息
    sub_by_tag = {f.number: f for f in fld.schema}
    return _decode_message(sub_by_tag, payload, 0, len(payload),
                           depth + 1, max_depth, max_length), pos


# ---------------------------------------------------------------- 编码

def encode(schema, message):
    """把 Message（或普通 dict，视为无未知字段）编码为 bytes。

    未知字段的原始字节追加在已知字段之后，逐字节保留。
    """
    if isinstance(message, Message):
        fields, unknown = message.fields, message.unknown
    else:
        fields, unknown = message, ()
    out = bytearray()
    for fld in schema:
        if fld.name not in fields:
            continue
        values = fields[fld.name] if fld.repeated else (fields[fld.name],)
        for value in values:
            _encode_field(out, fld, value)
    for raw in unknown:
        out += raw
    return bytes(out)


def _encode_field(out, fld, value):
    out += encode_varint(fld.number << 3 | _KIND_WIRE[fld.kind])
    kind = fld.kind
    if kind == 'varint':
        out += encode_varint(value)
    elif kind == 'fixed32':
        out += struct.pack('<I', value)
    elif kind == 'fixed64':
        out += struct.pack('<Q', value)
    elif kind == 'bytes':
        out += encode_varint(len(value))
        out += value
    elif kind == 'string':
        raw = value.encode('utf-8')
        out += encode_varint(len(raw))
        out += raw
    else:  # message
        raw = encode(fld.schema, value)
        out += encode_varint(len(raw))
        out += raw
