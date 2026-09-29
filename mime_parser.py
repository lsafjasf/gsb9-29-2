"""MIME 报文解析库（仅使用 Python 标准库）。

功能：
- 头部折叠展开、参数化头部解析（含 RFC 2231 续段 / 编码参数）
- RFC 2047 编码化头部（encoded-word）还原
- 嵌套 multipart/* 与 message/rfc822 递归解析，输出层级路径
- Content-Transfer-Encoding 还原（base64 / quoted-printable / 7bit / 8bit / binary）
- 字符集未知或解码失败时的显式降级链（见 README.md「降级策略」）
"""

from __future__ import annotations

import base64
import binascii
import codecs
import hashlib
import quopri
import re
import urllib.parse
from dataclasses import dataclass, field

__all__ = [
    "Part",
    "parse_message",
    "decode_header_value",
    "decode_text",
    "parse_structured_header",
    "dump_tree",
]

_HEADER_NAME_RE = re.compile(rb"[!-9;-~]+")
_ENCODED_WORD_RE = re.compile(r"=\?([^?\s]+)\?([bBqQ])\?([^?]*)\?=")
_PARAM_CONT_RE = re.compile(r"([^\*]+)\*(\d+)(\*?)$")
_QUOTED_ESCAPE_RE = re.compile(r"\\(.)")


# ---------------------------------------------------------------- 字符集降级

def _lookup_charset(charset):
    if not charset:
        return None
    try:
        return codecs.lookup(charset).name
    except (LookupError, TypeError, ValueError):
        return None


def decode_text(data, charset=None):
    """按显式降级链把字节还原为文本，返回 (text, strategy)。

    降级链：
      1. declared:<charset>  报文声明且本机认识的字符集
      2. utf-8               声明缺失 / 未知，或声明字符集解码失败
      3. latin-1-lossless    最终兜底，逐字节映射，绝不失败
    """
    if charset:
        name = _lookup_charset(charset)
        if name is not None:
            try:
                return data.decode(name), "declared:%s" % charset
            except (UnicodeDecodeError, ValueError):
                pass
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1-lossless"


# ---------------------------------------------------------------- RFC 2047 编码化头部

def _decode_encoded_word(charset, encoding, text):
    try:
        if encoding in ("b", "B"):
            raw = base64.b64decode(text, validate=False)
        else:
            raw = quopri.decodestring(text.replace("_", " "))
    except (binascii.Error, ValueError):
        return "=?%s?%s?%s?=" % (charset, encoding, text)
    return decode_text(raw, charset)[0]


def decode_header_value(value):
    """还原 RFC 2047 encoded-word；相邻 encoded-word 之间的空白被剔除。"""
    out = []
    pos = 0
    prev_word = False
    for match in _ENCODED_WORD_RE.finditer(value):
        gap = value[pos:match.start()]
        if not (prev_word and gap.strip(" \t\r\n") == ""):
            out.append(gap)
        out.append(_decode_encoded_word(match.group(1), match.group(2), match.group(3)))
        pos = match.end()
        prev_word = True
    out.append(value[pos:])
    return "".join(out)


# ---------------------------------------------------------------- 参数化头部（RFC 2231）

def _split_segments(value):
    """按 ';' 切分，忽略引号内的分号，保留反斜杠转义。"""
    segments, current = [], []
    in_quote = escaped = False
    for ch in value:
        if escaped:
            current.append(ch)
            escaped = False
        elif in_quote and ch == "\\":
            current.append(ch)
            escaped = True
        elif ch == '"':
            in_quote = not in_quote
            current.append(ch)
        elif ch == ";" and not in_quote:
            segments.append("".join(current))
            current = []
        else:
            current.append(ch)
    segments.append("".join(current))
    return segments


def _unquote(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return _QUOTED_ESCAPE_RE.sub(r"\1", text[1:-1])
    return text


def _split_2231_encoded(value):
    """把 charset'lang'text 拆成 (charset, 原始 text)。"""
    charset, _, rest = value.partition("'")
    _, _, encoded = rest.partition("'")
    return charset, encoded


def _decode_2231_single(value):
    charset, encoded = _split_2231_encoded(value)
    data = urllib.parse.unquote_to_bytes(encoded)
    return decode_text(data, charset or None)[0]


def _reassemble_2231(raw_params):
    simple, encoded_single, continuations = {}, {}, {}
    for name, val in raw_params:
        match = _PARAM_CONT_RE.match(name)
        if match:
            bucket = continuations.setdefault(match.group(1), {})
            bucket[int(match.group(2))] = (bool(match.group(3)), val)
        elif name.endswith("*"):
            encoded_single[name[:-1]] = val
        else:
            simple[name] = val
    params = dict(simple)
    for name, val in encoded_single.items():
        params[name] = _decode_2231_single(val)
    for name, segments in continuations.items():
        ordered = [segments[i] for i in sorted(segments)]
        first_encoded, first_val = ordered[0]
        if first_encoded:
            charset, encoded = _split_2231_encoded(first_val)
            data = urllib.parse.unquote_to_bytes(encoded)
            for flag, val in ordered[1:]:
                if flag:
                    data += urllib.parse.unquote_to_bytes(val)
                else:
                    data += val.encode("utf-8", "surrogateescape")
            params[name] = decode_text(data, charset or None)[0]
        else:
            params[name] = "".join(val for _, val in ordered)
    return params


def parse_structured_header(value):
    """解析 'main; k1=v1; k2="v2"' 形式的头部，返回 (main, params)。"""
    segments = _split_segments(value)
    main = segments[0].strip()
    raw_params = []
    for seg in segments[1:]:
        if "=" not in seg:
            continue
        key, val = seg.split("=", 1)
        key = key.strip().lower()
        if key:
            raw_params.append((key, _unquote(val)))
    return main, _reassemble_2231(raw_params)


# ---------------------------------------------------------------- 报文结构

@dataclass
class Part:
    """报文（子）树节点。叶子节点的 payload 为 CTE 解码后的字节。"""

    path: str
    headers: list = field(default_factory=list)   # [(name, value)]，value 已展开、未解码
    content_type: str = "text/plain"
    params: dict = field(default_factory=dict)
    cte: str = "7bit"
    disposition: str = ""
    disp_params: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    payload: bytes = b""
    defects: list = field(default_factory=list)

    @property
    def is_container(self):
        return bool(self.children)

    @property
    def is_attachment(self):
        return self.disposition == "attachment" or "filename" in self.disp_params

    def header(self, name, default=None):
        lname = name.lower()
        for hname, hval in self.headers:
            if hname.lower() == lname:
                return hval
        return default

    def decoded_header(self, name, default=None):
        raw = self.header(name)
        return default if raw is None else decode_header_value(raw)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def text(self):
        """text/* 叶子部分返回 (text, strategy)，其余返回 None。"""
        if self.is_container or not self.content_type.startswith("text/"):
            return None
        return decode_text(self.payload, self.params.get("charset"))

    def digest(self):
        return hashlib.sha256(self.payload).hexdigest()


# ---------------------------------------------------------------- 解析入口

def parse_message(raw):
    """解析一封报文，返回根 Part（path 为 "1"）。"""
    return _parse_part(bytes(raw), "1")


def _parse_part(raw, path):
    head, body = _split_head_body(raw)
    part = Part(path=path, headers=_unfold_headers(head))

    ct_raw = part.header("content-type")
    if ct_raw is not None:
        main, params = parse_structured_header(ct_raw)
        main = main.lower()
        if "/" in main:
            part.content_type = main
        else:
            part.defects.append("invalid-content-type:%r" % main)
        part.params = params

    cte_raw = part.header("content-transfer-encoding")
    if cte_raw is not None:
        part.cte = cte_raw.strip().lower()

    disp_raw = part.header("content-disposition")
    if disp_raw is not None:
        part.disposition, part.disp_params = parse_structured_header(disp_raw)
        part.disposition = part.disposition.lower()

    maintype = part.content_type.split("/", 1)[0]
    if maintype == "multipart":
        boundary = part.params.get("boundary")
        if boundary:
            chunks = _split_multipart(body, boundary)
            part.children = [
                _parse_part(chunk, "%s.%d" % (path, index + 1))
                for index, chunk in enumerate(chunks)
            ]
        else:
            part.defects.append("multipart-without-boundary")
            part.payload = _decode_cte(body, part.cte, part.defects)
    elif part.content_type == "message/rfc822":
        part.children = [_parse_part(body, path + ".1")]
    else:
        part.payload = _decode_cte(body, part.cte, part.defects)
    return part


def _split_head_body(raw):
    """切分头部与正文。

    头部结束于第一个空行；遇到非法头行（无冒号且非续行）时头部提前结束，
    其余内容全部归入正文。空报文得到空头 + 空正文。
    """
    lines = raw.splitlines(keepends=True)
    head_lines = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        stripped = line.rstrip(b"\r\n")
        if stripped == b"":
            idx += 1
            break
        if line[:1] in (b" ", b"\t"):
            if not head_lines:
                break  # 报文以续行开头：视为没有头部
            head_lines.append(line)
            idx += 1
            continue
        name, sep, _ = stripped.partition(b":")
        if sep and _HEADER_NAME_RE.fullmatch(name):
            head_lines.append(line)
            idx += 1
            continue
        break
    return b"".join(head_lines), b"".join(lines[idx:])


def _unfold_headers(head):
    """展开折叠头部，返回 [(name, value_str)]；续行前导空白按 RFC 保留。"""
    headers = []
    for line in head.splitlines():
        if line[:1] in (b" ", b"\t") and headers:
            name, value = headers[-1]
            headers[-1] = (name, value + line.decode("utf-8", "surrogateescape"))
        else:
            name, _, value = line.partition(b":")
            headers.append((
                name.decode("ascii", "surrogateescape"),
                value.decode("utf-8", "surrogateescape").strip(" \t"),
            ))
    return headers


def _split_multipart(body, boundary):
    """按 boundary 切分多部分正文；前导 / 结尾说明文字被丢弃。"""
    delimiter = b"--" + boundary.encode("utf-8", "surrogateescape")
    closing = delimiter + b"--"
    chunks = []
    current = None
    for line in body.splitlines(keepends=True):
        marker = line.rstrip(b"\r\n").rstrip(b" \t")
        if marker == delimiter or marker == closing:
            if current is not None:
                chunks.append(_strip_one_eol(b"".join(current)))
            current = None if marker == closing else []
            if marker == closing:
                break
            continue
        if current is not None:
            current.append(line)
    if current is not None:
        chunks.append(_strip_one_eol(b"".join(current)))
    return chunks


def _strip_one_eol(data):
    """分隔符前的那个换行属于分隔符本身，需从上一部分末尾剥掉。"""
    if data.endswith(b"\r\n"):
        return data[:-2]
    if data.endswith((b"\n", b"\r")):
        return data[:-1]
    return data


def _decode_cte(data, cte, defects):
    cte = (cte or "7bit").lower()
    if cte == "base64":
        cleaned = re.sub(rb"[^A-Za-z0-9+/=]", b"", data)
        if not cleaned:
            if data.strip():
                defects.append("invalid-base64-passthrough")
                return data
            return b""
        cleaned += b"=" * (-len(cleaned) % 4)
        try:
            return base64.b64decode(cleaned, validate=True)
        except (binascii.Error, ValueError):
            defects.append("invalid-base64-passthrough")
            return data
    if cte == "quoted-printable":
        return quopri.decodestring(data)
    if cte in ("7bit", "8bit", "binary"):
        return data
    defects.append("unknown-cte-passthrough:%s" % cte)
    return data


# ---------------------------------------------------------------- 展示

def dump_tree(part):
    """输出各部分的内容类型与层级路径（含附件摘要与降级信息）。"""
    lines = []
    for node in part.walk():
        indent = "  " * node.path.count(".")
        info = "%s %s" % (node.path, node.content_type)
        extras = []
        if node.is_attachment:
            extras.append("attachment filename=%r" % node.disp_params.get("filename"))
        if not node.is_container:
            extras.append("bytes=%d" % len(node.payload))
            extras.append("sha256=%s" % node.digest())
            decoded = node.text()
            if decoded is not None:
                extras.append("charset=%s" % decoded[1])
        if node.defects:
            extras.append("defects=%s" % ",".join(node.defects))
        if extras:
            info += "  [" + "; ".join(extras) + "]"
        lines.append(indent + info)
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        with open(sys.argv[1], "rb") as fh:
            raw_message = fh.read()
    else:
        raw_message = sys.stdin.buffer.read()
    print(dump_tree(parse_message(raw_message)))
