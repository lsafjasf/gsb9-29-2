"""严格 DER（X.690）TLV 解析。

只依赖标准库。所有长度字段按变长编码解析，绝不使用固定偏移；
对 DER 的严格性要求（最短长度编码、禁止不定长、整数最短编码等）
逐一检查并抛出带分类 code 的 DerError。
"""

from dataclasses import dataclass, field
from typing import List, Optional

from .errors import DerError

# 单个元素声明长度的硬上限，防止“超长字段”攻击导致内存问题。
MAX_ELEMENT_LENGTH = 16 * 1024 * 1024  # 16 MiB
# 长度字段本身最多 8 字节（DER 允许最多 126 字节，但超过 8 字节没有实际意义）。
MAX_LENGTH_OCTETS = 8
# 嵌套深度上限，防止恶意构造的超深嵌套耗尽栈。
MAX_DEPTH = 64

# 通用类标签号
TAG_BOOLEAN = 0x01
TAG_INTEGER = 0x02
TAG_BIT_STRING = 0x03
TAG_OCTET_STRING = 0x04
TAG_NULL = 0x05
TAG_OID = 0x06
TAG_UTF8STRING = 0x0C
TAG_PRINTABLESTRING = 0x13
TAG_TELETEXSTRING = 0x14
TAG_IA5STRING = 0x16
TAG_UTCTIME = 0x17
TAG_GENERALIZEDTIME = 0x18
TAG_SEQUENCE = 0x10
TAG_SET = 0x11

TAG_NAMES = {
    TAG_BOOLEAN: "BOOLEAN",
    TAG_INTEGER: "INTEGER",
    TAG_BIT_STRING: "BIT STRING",
    TAG_OCTET_STRING: "OCTET STRING",
    TAG_NULL: "NULL",
    TAG_OID: "OBJECT IDENTIFIER",
    TAG_UTF8STRING: "UTF8String",
    TAG_PRINTABLESTRING: "PrintableString",
    TAG_TELETEXSTRING: "TeletexString",
    TAG_IA5STRING: "IA5String",
    TAG_UTCTIME: "UTCTime",
    TAG_GENERALIZEDTIME: "GeneralizedTime",
    TAG_SEQUENCE: "SEQUENCE",
    TAG_SET: "SET",
}


@dataclass
class TLV:
    """一个已解析的 TLV 节点。constructed 节点的内容在 children 中。"""

    tag_class: int          # 0 universal, 1 application, 2 context, 3 private
    constructed: bool
    tag: int                # 标签号（不含类别/构造位）
    header_len: int         # 标签+长度编码的总字节数
    length: int             # 内容字节数
    content: bytes          # 原始内容字节（constructed 时为子元素的编码拼接）
    children: List["TLV"] = field(default_factory=list)
    offset: int = 0         # 相对整个输入缓冲区的起始偏移

    @property
    def content_offset(self) -> int:
        return self.offset + self.header_len

    @property
    def full(self) -> bytes:
        return self.content if not self.constructed else self.content

    def expect(self, tag, tag_class=0):
        if self.tag_class != tag_class or self.tag != tag:
            raise DerError(
                "期望标签 %s(class=%d)，实际是 tag=%d class=%d"
                % (TAG_NAMES.get(tag, tag), tag_class, self.tag, self.tag_class),
                code="der.unexpected_tag",
            )
        return self

    def child(self, index, tag=None, tag_class=0):
        if index >= len(self.children):
            raise DerError("子元素 #%d 不存在（共 %d 个）" % (index, len(self.children)),
                           code="der.missing_child")
        node = self.children[index]
        if tag is not None:
            node.expect(tag, tag_class)
        return node


def _read_tlv(data: bytes, pos: int, end: int, depth: int) -> "tuple[TLV, int]":
    """从 data[pos:end] 解析一个 TLV，返回 (tlv, 下一个位置)。"""
    if depth > MAX_DEPTH:
        raise DerError("嵌套深度超过上限 %d" % MAX_DEPTH, code="der.too_deep")
    if pos >= end:
        raise DerError("在偏移 %d 处期望标签字节，但数据已结束" % pos,
                       code="der.truncated")

    start = pos
    first = data[pos]
    pos += 1
    tag_class = first >> 6
    constructed = bool(first & 0x20)
    tag = first & 0x1F

    if tag == 0x1F:
        # 高标签号形式：base-128 变长编码
        tag = 0
        count = 0
        while True:
            if pos >= end:
                raise DerError("高标签号编码在偏移 %d 处被截断" % start,
                               code="der.truncated")
            byte = data[pos]
            pos += 1
            count += 1
            if count == 1 and byte == 0x80:
                raise DerError("高标签号首字节非法(0x80)", code="der.bad_tag")
            tag = (tag << 7) | (byte & 0x7F)
            if count > 5:
                raise DerError("高标签号编码过长", code="der.bad_tag")
            if not (byte & 0x80):
                break
        if tag < 31:
            raise DerError("标签 %d 应使用短形式编码" % tag, code="der.bad_tag")

    if pos >= end:
        raise DerError("在偏移 %d 处期望长度字节，但数据已结束" % pos,
                       code="der.truncated")

    len_byte = data[pos]
    pos += 1
    if len_byte < 0x80:
        length = len_byte
    else:
        num_octets = len_byte & 0x7F
        if num_octets == 0:
            raise DerError("DER 禁止使用不定长(indefinite length)编码",
                           code="der.indefinite_length")
        if num_octets > MAX_LENGTH_OCTETS:
            raise DerError("长度字段占用 %d 字节，超过上限 %d"
                           % (num_octets, MAX_LENGTH_OCTETS),
                           code="der.length_overflow")
        if pos + num_octets > end:
            raise DerError("长度字段在偏移 %d 处被截断" % pos, code="der.truncated")
        if data[pos] == 0x00:
            raise DerError("长度字段非最短编码（前导零字节）",
                           code="der.nonminimal_length")
        length = int.from_bytes(data[pos:pos + num_octets], "big")
        pos += num_octets
        if length < 0x80:
            raise DerError("长度 %d 应使用短形式编码" % length,
                           code="der.nonminimal_length")

    if length > MAX_ELEMENT_LENGTH:
        raise DerError("元素声明长度 %d 超过上限 %d"
                       % (length, MAX_ELEMENT_LENGTH),
                       code="der.length_overflow")
    if pos + length > end:
        raise DerError("偏移 %d 处声明长度 %d 超出剩余数据 %d 字节"
                       % (pos, length, end - pos),
                       code="der.truncated")

    content = data[pos:pos + length]
    header_len = pos - start
    content_start = pos
    pos += length

    tlv = TLV(tag_class, constructed, tag, header_len, length, content,
              offset=start)
    if constructed:
        # 子元素直接在原缓冲区上解析，保证 offset 是全局偏移
        tlv.children = _parse_all(data, content_start, content_start + length,
                                  depth + 1)
    return tlv, pos


def _parse_all(data: bytes, pos: int, end: int, depth: int) -> List[TLV]:
    items = []
    while pos < end:
        tlv, pos = _read_tlv(data, pos, end, depth)
        items.append(tlv)
    return items


def parse(data: bytes) -> TLV:
    """解析一段必须恰好包含一个完整 TLV 的 DER 数据。"""
    if not isinstance(data, (bytes, bytearray)):
        raise DerError("输入必须是 bytes", code="der.bad_input")
    data = bytes(data)
    if not data:
        raise DerError("输入为空", code="der.truncated")
    tlv, pos = _read_tlv(data, 0, len(data), 0)
    if pos != len(data):
        raise DerError("顶层元素之后还有 %d 字节多余数据" % (len(data) - pos),
                       code="der.trailing_data")
    return tlv


def parse_integer(tlv: TLV, signed: bool = False) -> int:
    """解析 INTEGER，并检查 DER 最短编码规则。"""
    tlv.expect(TAG_INTEGER)
    content = tlv.content
    if not content:
        raise DerError("INTEGER 内容为空", code="der.bad_integer")
    if len(content) > 1:
        if content[0] == 0x00 and not (content[1] & 0x80):
            raise DerError("INTEGER 非最短编码（多余前导 0x00）",
                           code="der.nonminimal_integer")
        if content[0] == 0xFF and (content[1] & 0x80):
            raise DerError("INTEGER 非最短编码（多余前导 0xFF）",
                           code="der.nonminimal_integer")
    return int.from_bytes(content, "big", signed=signed)


def parse_bit_string(tlv: TLV) -> bytes:
    """解析 BIT STRING，返回去掉填充位后的字节内容。"""
    tlv.expect(TAG_BIT_STRING)
    content = tlv.content
    if not content:
        raise DerError("BIT STRING 缺少未用位数字节", code="der.bad_bitstring")
    unused = content[0]
    if unused > 7:
        raise DerError("BIT STRING 未用位数 %d 非法" % unused,
                       code="der.bad_bitstring")
    body = content[1:]
    if not body and unused:
        raise DerError("空 BIT STRING 的未用位数必须为零", code="der.bad_bitstring")
    if body and unused and (body[-1] & ((1 << unused) - 1)):
        raise DerError("BIT STRING 填充位非零，违反 DER", code="der.bad_bitstring")
    return body


def parse_oid(tlv: TLV) -> str:
    """解析 OBJECT IDENTIFIER，返回点分十进制字符串。"""
    tlv.expect(TAG_OID)
    content = tlv.content
    if not content:
        raise DerError("OID 内容为空", code="der.bad_oid")
    arcs = []
    value = 0
    for byte in content:
        value = (value << 7) | (byte & 0x7F)
        if not (byte & 0x80):
            arcs.append(value)
            value = 0
    if value != 0 or (content[-1] & 0x80):
        raise DerError("OID 最后一个子标识符被截断", code="der.bad_oid")
    first = arcs[0]
    if first < 40:
        root = (0, first)
    elif first < 80:
        root = (1, first - 40)
    else:
        root = (2, first - 80)
    return ".".join(str(a) for a in (*root, *arcs[1:]))


def decode_any_string(tlv: TLV) -> str:
    """按标签解码 DirectoryString 类字段。"""
    raw = tlv.content
    if tlv.tag == TAG_UTF8STRING:
        return raw.decode("utf-8")
    if tlv.tag in (TAG_PRINTABLESTRING, TAG_IA5STRING):
        return raw.decode("ascii")
    if tlv.tag == TAG_TELETEXSTRING:
        # 实际证书中 T61 常被当作 latin-1 使用
        return raw.decode("latin-1")
    if tlv.tag == 0x1E:  # BMPString
        return raw.decode("utf-16-be")
    if tlv.tag == 0x1C:  # UniversalString
        return raw.decode("utf-32-be")
    raise DerError("不支持的字符串标签 %d" % tlv.tag, code="der.bad_string")
