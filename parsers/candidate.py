"""新写的解析器：与参照实现保持同一接口。

注意：本文件故意注入了两处与参照实现的差异，用于演示/自测差分框架：
  1. 深度判断差一格（>= 而非 >），在嵌套恰好 MAX_DEPTH 层时行为不同；
  2. UINT 长度上限判断为 >= 8，错误地拒绝 8 字节 UINT。
真实使用时把这里替换成待测的新实现即可。
"""

from difftest.errors import Category, ParseError

T_NODE, T_STR, T_UINT = 0x01, 0x02, 0x03
HEADER = 3
MAX_DEPTH = 32


def parse(data, max_depth=MAX_DEPTH):
    data = bytes(data)
    records, _ = _parse_seq(data, 0, len(data), 1, max_depth)
    return records


def _parse_seq(data, start, end, depth, max_depth):
    # 注入差异 1：参照实现是 depth > max_depth
    if depth >= max_depth:
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
            # 注入差异 2：参照实现是 length > 8
            if not 1 <= length < 8:
                raise ParseError(Category.UINT_BAD_LENGTH,
                                 f"uint payload must be 1..8 bytes, got {length}",
                                 pos)
            out.append({"type": "uint", "value": int.from_bytes(payload, "big")})
        else:
            raise ParseError(Category.UNKNOWN_TYPE, f"type 0x{rtype:02x}", pos)
        pos = pend
    return out, pos
