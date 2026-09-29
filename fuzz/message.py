"""CAFE-TLV 报文模型与序列化（纯标准库）。

报文格式:
  header: magic(2) = CA FE, version(1), flags(1), total_length(2, BE, 含头部)
  body:   field*
  field:  tag(1), length(2, BE), value(length)
    tag 0x10 INT       value = 4 字节大端无符号整数
    tag 0x11 STRING    value = 原始字节
    tag 0x12 CONTAINER value = 嵌套 field 序列
    tag 0x13 RAW       value = 原始字节
"""

MAGIC = b"\xca\xfe"
VERSION = 1
HEADER_LEN = 6

TAG_INT = 0x10
TAG_STR = 0x11
TAG_CONTAINER = 0x12
TAG_RAW = 0x13

MAX_SERIALIZE_DEPTH = 2000


class Field:
    __slots__ = ("tag", "value")

    def __init__(self, tag, value):
        self.tag = tag
        self.value = value  # INT:int / STR,RAW:bytes / CONTAINER:list[Field]

    def clone(self):
        """迭代式深拷贝，避免深层嵌套触发递归限制。"""
        if self.tag != TAG_CONTAINER:
            return Field(self.tag, self.value)
        root = Field(TAG_CONTAINER, [])
        stack = [(self, root)]
        while stack:
            src, dst = stack.pop()
            for child in src.value:
                if child.tag == TAG_CONTAINER:
                    cloned = Field(TAG_CONTAINER, [])
                    dst.value.append(cloned)
                    stack.append((child, cloned))
                else:
                    dst.value.append(Field(child.tag, child.value))
        return root

    def __repr__(self):
        if self.tag == TAG_CONTAINER:
            return f"Field(CONTAINER, {self.value!r})"
        return f"Field(0x{self.tag:02x}, {self.value!r})"


class Message:
    __slots__ = ("fields", "flags")

    def __init__(self, fields=None, flags=0):
        self.fields = fields if fields is not None else []
        self.flags = flags

    def clone(self):
        return Message([f.clone() for f in self.fields], self.flags)

    def depth(self):
        """迭代计算最大嵌套深度（根层为 0）。"""
        best = 0
        stack = [(f, 0) for f in self.fields]
        while stack:
            field, level = stack.pop()
            best = max(best, level)
            if field.tag == TAG_CONTAINER:
                stack.extend((c, level + 1) for c in field.value)
        return best


def serialize_with_layout(fields, flags=0):
    """序列化并返回 (bytes, length_field_offsets)。

    length_field_offsets 为所有长度字段（含头部 total_length）在输出中的
    字节偏移，供结构感知的长度篡改变异器使用。迭代实现，支持深层嵌套。
    """
    offsets = [HEADER_LEN - 2]  # 头部 total_length 偏移
    root_out = bytearray()
    # 帧: (子字段迭代器, 输出缓冲, 本层起始偏移, 容器 tag 或 None)
    stack = [(iter(fields), root_out, HEADER_LEN, None)]
    while stack:
        iterator, out, base, tag = stack[-1]
        try:
            field = next(iterator)
        except StopIteration:
            stack.pop()
            if tag is not None:
                payload = bytes(out)
                p_iter, p_out, p_base, _ = stack[-1]
                offsets.append(p_base + len(p_out) + 1)
                p_out += bytes([tag]) + (len(payload) & 0xFFFF).to_bytes(2, "big") + payload
            continue
        if field.tag == TAG_INT:
            payload = (field.value & 0xFFFFFFFF).to_bytes(4, "big")
            offsets.append(base + len(out) + 1)
            out += bytes([field.tag]) + (len(payload) & 0xFFFF).to_bytes(2, "big") + payload
        elif field.tag == TAG_CONTAINER:
            child_base = base + len(out) + 3
            stack.append((iter(field.value), bytearray(), child_base, field.tag))
        else:
            payload = bytes(field.value)
            offsets.append(base + len(out) + 1)
            out += bytes([field.tag]) + (len(payload) & 0xFFFF).to_bytes(2, "big") + payload
    total = HEADER_LEN + len(root_out)
    header = MAGIC + bytes([VERSION, flags & 0xFF]) + (total & 0xFFFF).to_bytes(2, "big")
    return bytes(header) + bytes(root_out), offsets


def serialize(message):
    data, _ = serialize_with_layout(message.fields, message.flags)
    return data


def parse_lenient(data):
    """宽容解析（供测试回读）：长度越界时截断，忽略尾部垃圾。迭代实现。"""
    if len(data) < HEADER_LEN or data[:2] != MAGIC:
        raise ValueError("not a CAFE-TLV message")
    flags = data[3]
    msg = Message([], flags)
    pos = HEADER_LEN
    stack = [(msg.fields, len(data))]
    while stack:
        out, end = stack[-1]
        if pos + 3 > end or pos + 3 > len(data):
            stack.pop()
            continue
        tag = data[pos]
        length = int.from_bytes(data[pos + 1:pos + 3], "big")
        avail = min(length, len(data) - pos - 3, end - pos - 3)
        if avail < 0:
            stack.pop()
            continue
        if tag == TAG_CONTAINER:
            children = []
            out.append(Field(TAG_CONTAINER, children))
            pos += 3
            stack.append((children, pos + 3 + avail))
            continue
        elif tag == TAG_INT:
            raw = data[pos + 3:pos + 3 + avail]
            out.append(Field(TAG_INT, int.from_bytes(raw, "big") if raw else 0))
        else:
            out.append(Field(tag, bytes(data[pos + 3:pos + 3 + avail])))
        pos += 3 + avail
    return msg
