"""参照实现：TLV 协议解析器（视为正确基准）。

报文格式（字节流，大端）：
    message := record*
    record  := type:1  len:2  payload:len
    type 0x01 NODE  payload 为嵌套的 record 序列
    type 0x02 STR   payload 为 UTF-8 文本
    type 0x03 UINT  payload 为 1..=8 字节大端无符号整数

解析结果为嵌套 dict 列表，可直接做 == 比较。
"""

from difftest.errors import Category, ParseError

T_NODE, T_STR, T_UINT = 0x01, 0x02, 0x03
HEADER = 3
MAX_DEPTH = 32  # 序列最多允许嵌套的层数（顶层为第 1 层）


def parse(data, max_depth=MAX_DEPTH):
    """解析整条报文，返回记录列表；失败抛 ParseError。"""
    data = bytes(data)
    records, _ = _parse_seq(data, 0, len(data), 1, max_depth)
    return records


def _parse_seq(data, start, end, depth, max_depth):
    if depth > max_depth:
        raise ParseError(Category.DEPTH_EXCEEDED,
                         f"nesting deeper than {max_depth}", start)
    out = []
    pos = start
    while pos < end:
        if end - pos < HEADER:
            raise ParseError(Category.TRUNCATED_HEADER,
                             f"need {HEADER} header bytes, got {end - pos}", pos)
        rtype = data[pos]
        length = (data[pos + 1] << 8) | data[pos + 2]
        pstart = pos + HEADER
        pend = pstart + length
        if pend > end:
            raise ParseError(Category.TRUNCATED_PAYLOAD,
                             f"declared {length} bytes, only {end - pstart} left",
                             pos)
        payload = data[pstart:pend]
        if rtype == T_NODE:
            children, _ = _parse_seq(data, pstart, pend, depth + 1, max_depth)
            out.append({"type": "node", "children": children})
        elif rtype == T_STR:
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ParseError(Category.INVALID_UTF8, str(exc), pstart)
            out.append({"type": "str", "value": text})
        elif rtype == T_UINT:
            if not 1 <= length <= 8:
                raise ParseError(Category.UINT_BAD_LENGTH,
                                 f"uint payload must be 1..8 bytes, got {length}",
                                 pos)
            out.append({"type": "uint", "value": int.from_bytes(payload, "big")})
        else:
            raise ParseError(Category.UNKNOWN_TYPE, f"type 0x{rtype:02x}", pos)
        pos = pend
    return out, pos
