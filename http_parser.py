"""HTTP 请求报文解析器（仅标准库）。

逐字节状态机解析请求行与报头，支持：
- 续行（obs-fold）：以 SP/HTAB 开头的行被合并到上一个报头值中；
- 重复字段合并：默认以 ", " 连接，Cookie 以 "; " 连接，Set-Cookie 不合并；
- 上限保护：请求行长度、报头数量、单个报头长度均有界，越界立即抛出
  可区分的异常并停止读取。

合并规则（详见 README.md）：
1. 报头名大小写不敏感，保留首次出现的原始大小写与顺序；
2. 重复字段按出现顺序合并：
   - 默认：", " 连接（RFC 7230 §3.2.2，等价于单行的逗号列表）；
   - Cookie："; " 连接（RFC 6265 §5.4）；
   - Set-Cookie：永不合并，每次出现都是独立条目；
3. 续行折叠为单个空格后再参与合并。
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Dict, List, Optional, Tuple

__all__ = [
    "ParseError",
    "EmptyRequest",
    "RequestLineTooLong",
    "MalformedRequestLine",
    "TooManyHeaders",
    "HeaderTooLong",
    "MalformedHeader",
    "BadContinuation",
    "UnexpectedEOF",
    "Request",
    "HTTPRequestParser",
    "parse_request",
    "DEFAULT_MAX_REQUEST_LINE",
    "DEFAULT_MAX_HEADERS",
    "DEFAULT_MAX_HEADER_LINE",
]

DEFAULT_MAX_REQUEST_LINE = 8192   # 请求行最大字节数（不含 CRLF）
DEFAULT_MAX_HEADERS = 100         # 报头字段最大数量（按首次出现计）
DEFAULT_MAX_HEADER_LINE = 8192    # 单个报头（名字+值+续行）最大字节数

# 不以 ", " 合并的字段：name -> 连接符（None 表示不合并）
_SPECIAL_MERGE: Dict[str, Optional[str]] = {
    "cookie": "; ",
    "set-cookie": None,
}

# RFC 7230 token 字符（报头名 / 方法名合法字符）
_TCHARS = frozenset(
    b"!#$%&'*+-.^_`|~"
    b"0123456789"
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    b"abcdefghijklmnopqrstuvwxyz"
)

_CR = 0x0D
_LF = 0x0A
_SP = 0x20
_HTAB = 0x09
_COLON = 0x3A


class ParseError(Exception):
    """所有解析错误的基类。"""


class EmptyRequest(ParseError):
    """报文为空或请求行为空。"""


class RequestLineTooLong(ParseError):
    """请求行超过长度上限。"""


class MalformedRequestLine(ParseError):
    """请求行格式非法（方法/目标/版本缺失或非法、裸 CR 等）。"""


class TooManyHeaders(ParseError):
    """报头数量超过上限。"""


class HeaderTooLong(ParseError):
    """单个报头（含续行）超过长度上限。"""


class MalformedHeader(ParseError):
    """报头格式非法（缺少冒号、非法字段名、裸 CR 等）。"""


class BadContinuation(MalformedHeader):
    """续行出现在任何报头之前。"""


class UnexpectedEOF(ParseError):
    """输入在报文完整之前结束。"""


class _State(Enum):
    REQUEST_LINE = auto()
    REQUEST_LINE_CR = auto()
    HEADER_LINE_START = auto()
    HEADER_NAME = auto()
    HEADER_VALUE = auto()
    HEADER_VALUE_CR = auto()
    HEADERS_END = auto()
    DONE = auto()


class Request:
    """解析结果。"""

    __slots__ = ("method", "target", "version", "headers")

    def __init__(
        self,
        method: str,
        target: str,
        version: Tuple[int, int],
        headers: List[Tuple[str, str]],
    ) -> None:
        self.method = method
        self.target = target
        self.version = version
        # 有序列表 [(原始大小写名字, 合并后的值), ...]
        self.headers = headers

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        """大小写不敏感地取第一个匹配报头的值。"""
        lname = name.lower()
        for hname, hvalue in self.headers:
            if hname.lower() == lname:
                return hvalue
        return default

    def get_all(self, name: str) -> List[str]:
        """取所有匹配条目（仅 Set-Cookie 这类不合并字段会有多条）。"""
        lname = name.lower()
        return [v for n, v in self.headers if n.lower() == lname]

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return (
            f"Request(method={self.method!r}, target={self.target!r}, "
            f"version={self.version!r}, headers={self.headers!r})"
        )


class HTTPRequestParser:
    """逐字节增量解析器。feed() 遇错立即抛出并停止读取。"""

    def __init__(
        self,
        max_request_line: int = DEFAULT_MAX_REQUEST_LINE,
        max_headers: int = DEFAULT_MAX_HEADERS,
        max_header_line: int = DEFAULT_MAX_HEADER_LINE,
    ) -> None:
        if max_request_line <= 0 or max_headers <= 0 or max_header_line <= 0:
            raise ValueError("limits must be positive")
        self.max_request_line = max_request_line
        self.max_headers = max_headers
        self.max_header_line = max_header_line

        self._state = _State.REQUEST_LINE
        self._req_line = bytearray()
        self._hdr_name = bytearray()
        self._hdr_value = bytearray()
        self._hdr_size = 0            # 当前报头 名字+值+续行 的累计字节数
        self._hdr_count = 0           # 已开始解析的报头数（含重复）
        self._skip_ws = False         # 值起始/续行后跳过前导空白
        self._headers: List[Tuple[str, str]] = []
        self._index: Dict[str, int] = {}  # lower(name) -> self._headers 下标
        self.request: Optional[Request] = None

    # ------------------------------------------------------------------ API

    @property
    def done(self) -> bool:
        return self._state is _State.DONE

    def feed(self, data: bytes) -> None:
        """喂入字节流；解析出错或越界时立即抛异常，之后的字节不再读取。"""
        if self.done:
            raise ParseError("feed() called after message complete")
        for byte in data:
            self._step(byte)
            if self.done:
                break

    def finish(self) -> Request:
        """输入结束时调用，返回 Request；报文不完整则抛 UnexpectedEOF。"""
        if not self.done:
            if self._state is _State.REQUEST_LINE and not self._req_line:
                raise EmptyRequest("empty input")
            raise UnexpectedEOF(f"input ended in state {self._state.name}")
        assert self.request is not None
        return self.request

    # ---------------------------------------------------------- 状态机核心

    def _step(self, byte: int) -> None:
        state = self._state

        if state is _State.REQUEST_LINE:
            if byte == _CR:
                self._state = _State.REQUEST_LINE_CR
            elif byte == _LF:
                self._finish_request_line()
            else:
                if len(self._req_line) >= self.max_request_line:
                    raise RequestLineTooLong(
                        f"request line exceeds {self.max_request_line} bytes"
                    )
                self._req_line.append(byte)

        elif state is _State.REQUEST_LINE_CR:
            if byte != _LF:
                raise MalformedRequestLine("bare CR in request line")
            self._finish_request_line()

        elif state is _State.HEADER_LINE_START:
            if byte == _CR:
                self._state = _State.HEADERS_END
            elif byte == _LF:
                self._finish_headers()
            elif byte in (_SP, _HTAB):
                # 续行：折叠为单个空格
                if self._hdr_count == 0:
                    raise BadContinuation("continuation line before any header")
                self._grow_header(1)
                self._hdr_value += b" "
                self._skip_ws = True
                self._state = _State.HEADER_VALUE
            else:
                self._start_header(byte)

        elif state is _State.HEADER_NAME:
            if byte == _COLON:
                self._skip_ws = True
                self._state = _State.HEADER_VALUE
            elif byte in (_CR, _LF):
                raise MalformedHeader("header line missing ':'")
            elif byte in _TCHARS:
                self._grow_header(1)
                self._hdr_name.append(byte)
            else:
                raise MalformedHeader(
                    f"invalid byte 0x{byte:02x} in header name"
                )

        elif state is _State.HEADER_VALUE:
            if byte == _CR:
                self._state = _State.HEADER_VALUE_CR
            elif byte == _LF:
                self._state = _State.HEADER_LINE_START
            else:
                if self._skip_ws and byte in (_SP, _HTAB):
                    return  # 跳过冒号后/续行后的前导空白
                self._skip_ws = False
                self._grow_header(1)
                self._hdr_value.append(byte)

        elif state is _State.HEADER_VALUE_CR:
            if byte != _LF:
                raise MalformedHeader("bare CR in header value")
            self._state = _State.HEADER_LINE_START

        elif state is _State.HEADERS_END:
            if byte != _LF:
                raise MalformedHeader("bare CR after headers")
            self._finish_headers()

    # ------------------------------------------------------------- 辅助逻辑

    def _grow_header(self, n: int) -> None:
        self._hdr_size += n
        if self._hdr_size > self.max_header_line:
            raise HeaderTooLong(
                f"header exceeds {self.max_header_line} bytes"
            )

    def _start_header(self, first_byte: int) -> None:
        self._commit_header()
        self._hdr_count += 1
        if self._hdr_count > self.max_headers:
            raise TooManyHeaders(f"more than {self.max_headers} headers")
        if first_byte not in _TCHARS:
            raise MalformedHeader(
                f"invalid byte 0x{first_byte:02x} at header name start"
            )
        self._hdr_name = bytearray((first_byte,))
        self._hdr_value = bytearray()
        self._hdr_size = 1
        self._state = _State.HEADER_NAME

    def _commit_header(self) -> None:
        """把当前报头按合并规则写入结果表。"""
        if self._hdr_count == 0 or not self._hdr_name:
            return
        name = self._hdr_name.decode("ascii")
        value = self._hdr_value.decode("latin-1").rstrip(" \t")
        lname = name.lower()
        joiner = _SPECIAL_MERGE.get(lname, ", ")
        if joiner is None:
            self._headers.append((name, value))  # 不合并（Set-Cookie）
        elif lname in self._index:
            idx = self._index[lname]
            old_name, old_value = self._headers[idx]
            self._headers[idx] = (old_name, old_value + joiner + value)
        else:
            self._index[lname] = len(self._headers)
            self._headers.append((name, value))

    def _finish_request_line(self) -> None:
        raw = bytes(self._req_line)
        if not raw:
            raise EmptyRequest("empty request line")
        parts = raw.split(b" ")
        if len(parts) != 3 or any(not p for p in parts):
            raise MalformedRequestLine(
                "request line must be 'METHOD SP TARGET SP VERSION'"
            )
        method_b, target_b, version_b = parts
        if not all(c in _TCHARS for c in method_b):
            raise MalformedRequestLine("invalid method")
        if any(c < 0x21 or c == 0x7F for c in target_b):
            raise MalformedRequestLine("invalid character in request target")
        ver_parts = version_b[5:].split(b".") if version_b.startswith(b"HTTP/") else []
        if (
            len(ver_parts) != 2
            or not ver_parts[0].isdigit()
            or not ver_parts[1].isdigit()
        ):
            raise MalformedRequestLine("invalid HTTP version")
        major_s, minor_s = ver_parts
        self.request = Request(
            method=method_b.decode("ascii"),
            target=target_b.decode("latin-1"),
            version=(int(major_s), int(minor_s)),
            headers=[],
        )
        self._state = _State.HEADER_LINE_START

    def _finish_headers(self) -> None:
        self._commit_header()
        assert self.request is not None
        self.request.headers = self._headers
        self._state = _State.DONE


def parse_request(
    data: bytes,
    *,
    max_request_line: int = DEFAULT_MAX_REQUEST_LINE,
    max_headers: int = DEFAULT_MAX_HEADERS,
    max_header_line: int = DEFAULT_MAX_HEADER_LINE,
) -> Request:
    """一次性解析完整请求报文（请求行 + 报头，不含消息体）。"""
    parser = HTTPRequestParser(max_request_line, max_headers, max_header_line)
    parser.feed(data)
    return parser.finish()
