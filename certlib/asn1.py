"""ASN.1 DER 变长嵌套编码的严格解码器（标准库实现）。

证书字段是 TLV 变长嵌套结构：标识符 + 长度（短格式/长格式）+ 值，
值本身可以是嵌套 TLV 序列。按固定偏移读取必然错位，必须递归解析。

同一模块也提供最小 DER 编码器，仅用于自测固件生成。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import EncodingError, FailureCode

# 单字段长度上限（16 MiB），防止畸形长度声明造成资源耗尽。
MAX_FIELD_LENGTH = 1 << 24
# 长格式长度最多允许的后续字节数。
MAX_LENGTH_BYTES = 8

# Universal tag 编号。
TAG_BOOLEAN = 0x01
TAG_INTEGER = 0x02
TAG_BIT_STRING = 0x03
TAG_OCTET_STRING = 0x04
TAG_NULL = 0x05
TAG_OID = 0x06
TAG_UTF8_STRING = 0x0C
TAG_PRINTABLE_STRING = 0x13
TAG_T61_STRING = 0x14
TAG_IA5_STRING = 0x16
TAG_UTC_TIME = 0x17
TAG_GENERALIZED_TIME = 0x18
TAG_BMP_STRING = 0x1E

CONSTRUCTED = 0x20


@dataclass(frozen=True)
class Node:
    """一个已解析的 TLV 节点。

    tag: 完整首字节（类别位 + 构造位 + tag 号）。
    value: V 部分的原始字节。
    children: 构造类型时为递归解析出的子节点，否则为 None。
    start/end: 原始缓冲区中的字节范围（含 T 和 L）。
    """

    tag: int
    value: bytes
    children: tuple["Node", ...] | None
    start: int
    end: int

    @property
    def tag_number(self) -> int:
        return self.tag & 0x1F

    @property
    def constructed(self) -> bool:
        return bool(self.tag & CONSTRUCTED)

    def expect(self, tag: int, what: str = "") -> "Node":
        if self.tag != tag:
            raise EncodingError(
                FailureCode.UNEXPECTED_TAG,
                f"期望 tag 0x{tag:02X}（{what}），实际 0x{self.tag:02X}，"
                f"偏移 {self.start}",
            )
        return self

    def child_values(self) -> tuple["Node", ...]:
        if self.children is None:
            raise EncodingError(
                FailureCode.UNEXPECTED_TAG,
                f"tag 0x{self.tag:02X} 不是构造类型，无法取子节点",
            )
        return self.children


def _read_tag(data: bytes, pos: int) -> tuple[int, int]:
    """读取标识符，返回 (完整 tag 表示, 下一位置)。

    只支持 X.509 实际使用的单字节 tag（tag 号 < 31）；
    高位 tag 号在证书里不应出现，出现即拒绝。
    """
    if pos >= len(data):
        raise EncodingError(FailureCode.TRUNCATED, f"偏移 {pos}: 缺少 tag 字节")
    first = data[pos]
    if (first & 0x1F) == 0x1F:
        raise EncodingError(
            FailureCode.UNEXPECTED_TAG,
            f"偏移 {pos}: 不支持多字节（高位号）tag 0x{first:02X}",
        )
    return first, pos + 1


def _read_length(data: bytes, pos: int) -> tuple[int, int]:
    """读取 DER 长度（短格式 / 长格式），做严格性检查。"""
    if pos >= len(data):
        raise EncodingError(FailureCode.TRUNCATED, f"偏移 {pos}: 缺少长度字节")
    first = data[pos]
    pos += 1
    if first < 0x80:
        return first, pos
    if first == 0x80:
        raise EncodingError(
            FailureCode.INDEFINITE_LENGTH,
            f"偏移 {pos - 1}: DER 禁止不定长（0x80）编码",
        )
    n = first & 0x7F
    if n > MAX_LENGTH_BYTES:
        raise EncodingError(
            FailureCode.LENGTH_OVERFLOW,
            f"偏移 {pos - 1}: 长度字段含 {n} 个后续字节，超过上限 {MAX_LENGTH_BYTES}",
        )
    if pos + n > len(data):
        raise EncodingError(
            FailureCode.TRUNCATED,
            f"偏移 {pos}: 长度字段需要 {n} 字节，数据不足",
        )
    length_bytes = data[pos:pos + n]
    # 最小编码：首字节高位必须为 1；单字节长度不得用长格式。
    if length_bytes[0] == 0x00:
        raise EncodingError(
            FailureCode.NON_MINIMAL_LENGTH,
            f"偏移 {pos}: 长度长格式存在前导 0x00，非最小编码",
        )
    length = 0
    for b in length_bytes:
        length = (length << 8) | b
    pos += n
    if n == 1 and length < 0x80:
        raise EncodingError(
            FailureCode.NON_MINIMAL_LENGTH,
            f"偏移 {pos}: 长度 {length} 应使用短格式",
        )
    if length > MAX_FIELD_LENGTH:
        raise EncodingError(
            FailureCode.FIELD_TOO_LONG,
            f"偏移 {pos}: 声明长度 {length} 超过上限 {MAX_FIELD_LENGTH}",
        )
    return length, pos


def decode_one(data: bytes, pos: int = 0) -> tuple[Node, int]:
    """解析单个 TLV，返回 (节点, 下一偏移)。"""
    start = pos
    tag, pos = _read_tag(data, pos)
    length, pos = _read_length(data, pos)
    if length > len(data) - pos:
        raise EncodingError(
            FailureCode.TRUNCATED,
            f"偏移 {pos}: 值声明 {length} 字节，剩余 {len(data) - pos} 字节",
        )
    value = data[pos:pos + length]
    end = pos + length

    children: tuple[Node, ...] | None = None
    if tag & CONSTRUCTED:
        # 在原始缓冲上递归解析，子节点偏移保持为绝对偏移。
        parsed: list[Node] = []
        sub = pos
        while sub < end:
            child, sub = decode_one(data, sub)
            parsed.append(child)
        if sub != end:
            raise EncodingError(
                FailureCode.TRUNCATED,
                f"偏移 {start}: 构造类型子元素解析后长度不一致",
            )
        children = tuple(parsed)
    return Node(tag=tag, value=value, children=children, start=start, end=end), end


def decode_exact(data: bytes) -> Node:
    """解析整段数据：恰好一个完整 TLV，末尾不允许有多余字节。"""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise EncodingError(FailureCode.TRUNCATED, "输入必须是字节序列")
    data = bytes(data)
    if not data:
        raise EncodingError(FailureCode.TRUNCATED, "空数据，无可解析 TLV")
    node, end = decode_one(data, 0)
    if end != len(data):
        raise EncodingError(
            FailureCode.TRAILING_DATA,
            f"偏移 {end}: 顶层 TLV 之后仍有 {len(data) - end} 个多余字节",
        )
    return node


# ---- 基本类型值解码器（含严格性检查）----

def decode_integer(node: Node, what: str = "INTEGER") -> int:
    node.expect(TAG_INTEGER, what)
    raw = node.value
    if not raw:
        raise EncodingError(
            FailureCode.TRUNCATED,
            f"偏移 {node.start}: {what} 内容为空",
        )
    if len(raw) >= 2:
        if raw[0] == 0x00 and not (raw[1] & 0x80):
            raise EncodingError(
                FailureCode.NON_MINIMAL_INTEGER,
                f"偏移 {node.start}: {what} 存在冗余前导 0x00",
            )
        if raw[0] == 0xFF and (raw[1] & 0x80):
            raise EncodingError(
                FailureCode.NON_MINIMAL_INTEGER,
                f"偏移 {node.start}: {what} 存在冗余前导 0xFF",
            )
    return int.from_bytes(raw, signed=True)


def decode_boolean(node: Node, what: str = "BOOLEAN") -> bool:
    node.expect(TAG_BOOLEAN, what)
    if len(node.value) != 1:
        raise EncodingError(
            FailureCode.TRUNCATED, f"偏移 {node.start}: BOOLEAN 长度必须为 1"
        )
    if node.value[0] not in (0x00, 0xFF):
        raise EncodingError(
            FailureCode.NON_MINIMAL_INTEGER,
            f"偏移 {node.start}: BOOLEAN 的 DER 值只能是 0x00/0xFF",
        )
    return node.value[0] == 0xFF


def decode_bitstring(node: Node, what: str = "BIT STRING") -> bytes:
    """返回去掉未用位计数后的比特串内容字节。"""
    node.expect(TAG_BIT_STRING, what)
    if not node.value:
        raise EncodingError(
            FailureCode.INVALID_BITSTRING,
            f"偏移 {node.start}: BIT STRING 缺少未用位计数字节",
        )
    unused = node.value[0]
    if unused > 7:
        raise EncodingError(
            FailureCode.INVALID_BITSTRING,
            f"偏移 {node.start}: 未用位数 {unused} 非法（应 0..7）",
        )
    content = node.value[1:]
    if unused and not content:
        raise EncodingError(
            FailureCode.INVALID_BITSTRING,
            f"偏移 {node.start}: 声明 {unused} 个未用位但没有内容字节",
        )
    if unused and content[-1] & ((1 << unused) - 1):
        raise EncodingError(
            FailureCode.INVALID_BITSTRING,
            f"偏移 {node.start}: 末尾 {unused} 个未用位必须清零",
        )
    return content


def decode_oid(node: Node, what: str = "OID") -> str:
    node.expect(TAG_OID, what)
    raw = node.value
    if not raw:
        raise EncodingError(
            FailureCode.INVALID_OID, f"偏移 {node.start}: OID 内容为空"
        )
    arcs = [raw[0] // 40, raw[0] % 40]
    if arcs[0] > 2:
        arcs[0] = 2
        arcs[1] = raw[0] - 80
    value = 0
    started = False
    for i, b in enumerate(raw[1:], start=1):
        if not started and b == 0x80:
            raise EncodingError(
                FailureCode.INVALID_OID,
                f"偏移 {node.start}: OID 分量存在非最小编码（前导 0x80）",
            )
        started = True
        value = (value << 7) | (b & 0x7F)
        if not (b & 0x80):
            arcs.append(value)
            value = 0
            started = False
    if started:
        raise EncodingError(
            FailureCode.INVALID_OID,
            f"偏移 {node.start}: OID 末尾分量缺少结束字节",
        )
    return ".".join(str(a) for a in arcs)


def decode_string(node: Node, what: str = "字符串") -> str:
    t = node.tag
    if t in (TAG_PRINTABLE_STRING, TAG_IA5_STRING, TAG_T61_STRING):
        try:
            return node.value.decode("ascii")
        except UnicodeDecodeError as exc:
            raise EncodingError(
                FailureCode.INVALID_UTF8,
                f"偏移 {node.start}: {what} 非 ASCII: {exc}",
            ) from exc
    if t == TAG_UTF8_STRING:
        try:
            return node.value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EncodingError(
                FailureCode.INVALID_UTF8,
                f"偏移 {node.start}: {what} 非合法 UTF-8: {exc}",
            ) from exc
    if t == TAG_BMP_STRING:
        try:
            return node.value.decode("utf-16-be")
        except UnicodeDecodeError as exc:
            raise EncodingError(
                FailureCode.INVALID_UTF8,
                f"偏移 {node.start}: {what} 非合法 UTF-16: {exc}",
            ) from exc
    raise EncodingError(
        FailureCode.UNEXPECTED_TAG,
        f"偏移 {node.start}: 未知字符串 tag 0x{t:02X}（{what}）",
    )


# ---- 最小 DER 编码器（自测固件用）----

def encode_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    body = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def tlv(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + encode_length(len(content)) + content


def sequence(*items: bytes) -> bytes:
    return tlv(0x30, b"".join(items))


def set_of(*items: bytes) -> bytes:
    return tlv(0x31, b"".join(items))


def integer(value: int) -> bytes:
    if value == 0:
        body = b"\x00"
    elif value > 0:
        body = value.to_bytes((value.bit_length() + 7) // 8, "big")
        if body[0] & 0x80:
            body = b"\x00" + body
    else:
        length = max(1, (value.bit_length() + 7) // 8)
        body = value.to_bytes(length, "big", signed=True)
    return tlv(TAG_INTEGER, body)


def boolean(value: bool) -> bytes:
    return tlv(TAG_BOOLEAN, b"\xff" if value else b"\x00")


def bit_string(content: bytes, unused_bits: int = 0) -> bytes:
    return tlv(TAG_BIT_STRING, bytes([unused_bits]) + content)


def octet_string(content: bytes) -> bytes:
    return tlv(TAG_OCTET_STRING, content)


def null() -> bytes:
    return tlv(TAG_NULL, b"")


def oid(value: str) -> bytes:
    arcs = [int(p) for p in value.split(".")]
    first = 40 * arcs[0] + arcs[1]
    if arcs[0] == 2:
        first = 80 + arcs[1]
    out = [first]
    for arc in arcs[2:]:
        parts = [arc & 0x7F]
        arc >>= 7
        while arc:
            parts.append((arc & 0x7F) | 0x80)
            arc >>= 7
        out.extend(reversed(parts))
    return tlv(TAG_OID, bytes(out))


def utf8_string(value: str) -> bytes:
    return tlv(TAG_UTF8_STRING, value.encode("utf-8"))


def printable_string(value: str) -> bytes:
    return tlv(TAG_PRINTABLE_STRING, value.encode("ascii"))


def ia5_string(value: str) -> bytes:
    return tlv(TAG_IA5_STRING, value.encode("ascii"))


def utc_time(dt_text: str) -> bytes:
    # dt_text: YYMMDDHHMMSSZ
    return tlv(TAG_UTC_TIME, dt_text.encode("ascii"))


def generalized_time(dt_text: str) -> bytes:
    # dt_text: YYYYMMDDHHMMSSZ
    return tlv(TAG_GENERALIZED_TIME, dt_text.encode("ascii"))
