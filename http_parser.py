"""Incremental HTTP request-line and header parser."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple

MAX_REQUEST_LINE_LENGTH = 8190
MAX_HEADERS = 100
MAX_HEADER_LENGTH = 8190

_TOKEN_CHARS = frozenset(b"!#$%&'*+-.^_`|~")
_VALUE_FORBIDDEN = frozenset(range(0x00, 0x09)) | frozenset(range(0x0A, 0x20)) | {0x7F}


class ParseErrorCode(str, Enum):
    EMPTY_REQUEST = "EMPTY_REQUEST"
    INCOMPLETE_REQUEST = "INCOMPLETE_REQUEST"
    REQUEST_LINE_TOO_LONG = "REQUEST_LINE_TOO_LONG"
    TOO_MANY_HEADERS = "TOO_MANY_HEADERS"
    HEADER_TOO_LONG = "HEADER_TOO_LONG"
    MALFORMED_REQUEST_LINE = "MALFORMED_REQUEST_LINE"
    MALFORMED_HEADER = "MALFORMED_HEADER"
    INVALID_CHARACTER = "INVALID_CHARACTER"
    UNEXPECTED_DATA = "UNEXPECTED_DATA"


class HttpParseError(ValueError):
    def __init__(self, code: ParseErrorCode, message: str, offset: int):
        super().__init__(f"{code.value} at byte {offset}: {message}")
        self.code = code
        self.offset = offset


@dataclass(frozen=True)
class HttpRequest:
    method: str
    target: str
    version: str
    headers: Dict[str, str]


class _State(Enum):
    REQUEST_LINE = auto()
    REQUEST_LINE_LF = auto()
    HEADER_START = auto()
    HEADER_NAME = auto()
    HEADER_VALUE = auto()
    HEADER_VALUE_LF = auto()
    HEADER_END_LF = auto()
    DONE = auto()
    ERROR = auto()


class HttpRequestParser:
    """Parse one HTTP request head one byte at a time."""

    def __init__(
        self,
        *,
        max_request_line_length: int = MAX_REQUEST_LINE_LENGTH,
        max_headers: int = MAX_HEADERS,
        max_header_length: int = MAX_HEADER_LENGTH,
    ) -> None:
        if max_request_line_length < 1:
            raise ValueError("max_request_line_length must be positive")
        if max_headers < 0:
            raise ValueError("max_headers must be non-negative")
        if max_header_length < 1:
            raise ValueError("max_header_length must be positive")

        self.max_request_line_length = max_request_line_length
        self.max_headers = max_headers
        self.max_header_length = max_header_length

        self._state = _State.REQUEST_LINE
        self._offset = 0
        self._error: Optional[HttpParseError] = None
        self._result: Optional[HttpRequest] = None

        self._request_line = bytearray()
        self._method = ""
        self._target = ""
        self._version = ""

        self._line = bytearray()
        self._line_is_continuation = False
        self._current_header_size = 0
        self._header_count = 0
        self._fields: List[Tuple[str, str]] = []
        self._open_field_index: Optional[int] = None

    @property
    def done(self) -> bool:
        return self._state is _State.DONE

    @property
    def failed(self) -> bool:
        return self._state is _State.ERROR

    @property
    def offset(self) -> int:
        return self._offset

    def feed(self, data: bytes) -> Optional[HttpRequest]:
        """Consume bytes and return the request when its head is complete."""
        if isinstance(data, (bytearray, memoryview)):
            data = bytes(data)
        if not isinstance(data, bytes):
            raise TypeError("feed() requires a bytes-like object")

        if self._state is _State.ERROR:
            raise self._error  # type: ignore[misc]
        if self._state is _State.DONE:
            if data:
                self._fail(ParseErrorCode.UNEXPECTED_DATA, "data after request head")
            return self._result

        for index, byte in enumerate(data):
            self._consume_byte(byte)
            self._offset += 1
            if self._state is _State.DONE:
                if index != len(data) - 1:
                    self._fail(
                        ParseErrorCode.UNEXPECTED_DATA,
                        "data after request head",
                        offset=self._offset,
                    )
                return self._result
        return None

    def finish(self) -> HttpRequest:
        """Finish at EOF, returning the request or raising a stable error."""
        if self._state is _State.ERROR:
            raise self._error  # type: ignore[misc]
        if self._state is _State.DONE:
            return self._result  # type: ignore[return-value]
        if self._offset == 0:
            self._fail(ParseErrorCode.EMPTY_REQUEST, "empty request")
        self._fail(ParseErrorCode.INCOMPLETE_REQUEST, "EOF before end of headers")
        raise AssertionError("unreachable")

    def _consume_byte(self, byte: int) -> None:
        if self._state is _State.REQUEST_LINE:
            self._consume_request_line_byte(byte)
        elif self._state is _State.REQUEST_LINE_LF:
            self._expect_lf(byte, ParseErrorCode.MALFORMED_REQUEST_LINE)
            self._parse_request_line()
            self._state = _State.HEADER_START
        elif self._state is _State.HEADER_START:
            self._consume_header_start_byte(byte)
        elif self._state is _State.HEADER_NAME:
            self._consume_header_name_byte(byte)
        elif self._state is _State.HEADER_VALUE:
            self._consume_header_value_byte(byte)
        elif self._state is _State.HEADER_VALUE_LF:
            self._expect_lf(byte, ParseErrorCode.MALFORMED_HEADER)
            self._complete_header_line()
            self._state = _State.HEADER_START
        elif self._state is _State.HEADER_END_LF:
            self._expect_lf(byte, ParseErrorCode.MALFORMED_HEADER)
            self._finish_request()
        else:
            raise AssertionError(f"unexpected parser state: {self._state}")

    def _consume_request_line_byte(self, byte: int) -> None:
        if byte == 0x0D:
            self._state = _State.REQUEST_LINE_LF
            return
        if byte == 0x0A:
            self._fail(ParseErrorCode.MALFORMED_REQUEST_LINE, "bare LF in request line")
        if byte < 0x20 or byte > 0x7E:
            self._fail(
                ParseErrorCode.INVALID_CHARACTER,
                "request line must contain printable ASCII only",
            )
        if len(self._request_line) >= self.max_request_line_length:
            self._fail(
                ParseErrorCode.REQUEST_LINE_TOO_LONG,
                f"request line exceeds {self.max_request_line_length} bytes",
            )
        self._request_line.append(byte)

    def _expect_lf(self, byte: int, code: ParseErrorCode) -> None:
        if byte != 0x0A:
            self._fail(code, "CR must be followed by LF")

    def _consume_header_start_byte(self, byte: int) -> None:
        if byte == 0x0D:
            self._state = _State.HEADER_END_LF
            return
        if byte == 0x0A:
            self._fail(ParseErrorCode.MALFORMED_HEADER, "bare LF in header block")
        if byte in (0x20, 0x09):
            if self._open_field_index is None:
                self._fail(
                    ParseErrorCode.MALFORMED_HEADER,
                    "continuation line without a preceding header",
                )
            self._line_is_continuation = True
            self._begin_continuation(byte)
            self._state = _State.HEADER_VALUE
            return

        if self._header_count >= self.max_headers:
            self._fail(
                ParseErrorCode.TOO_MANY_HEADERS,
                f"more than {self.max_headers} header fields",
            )
        if not self._is_token_byte(byte):
            self._fail(
                ParseErrorCode.INVALID_CHARACTER,
                "header name contains an invalid character",
            )
        self._line_is_continuation = False
        self._current_header_size = 0
        self._line.clear()
        self._append_header_byte(byte)
        self._state = _State.HEADER_NAME

    def _consume_header_name_byte(self, byte: int) -> None:
        if byte == 0x3A:
            if not self._line:
                self._fail(ParseErrorCode.MALFORMED_HEADER, "empty header name")
            self._append_header_byte(byte)
            self._state = _State.HEADER_VALUE
            return
        if byte in (0x0D, 0x0A):
            self._fail(ParseErrorCode.MALFORMED_HEADER, "header line is missing ':'")
        if not self._is_token_byte(byte):
            self._fail(
                ParseErrorCode.INVALID_CHARACTER,
                "header name contains an invalid character",
            )
        self._append_header_byte(byte)

    def _consume_header_value_byte(self, byte: int) -> None:
        if byte == 0x0D:
            self._state = _State.HEADER_VALUE_LF
            return
        if byte == 0x0A:
            self._fail(ParseErrorCode.MALFORMED_HEADER, "bare LF in header line")
        if byte in _VALUE_FORBIDDEN:
            self._fail(
                ParseErrorCode.INVALID_CHARACTER,
                "header value contains a forbidden control character",
            )
        self._append_header_byte(byte)

    def _parse_request_line(self) -> None:
        parts = bytes(self._request_line).split(b" ")
        if len(parts) != 3 or any(not part for part in parts):
            self._fail(
                ParseErrorCode.MALFORMED_REQUEST_LINE,
                "request line must be 'METHOD SP target SP HTTP/x.y'",
            )

        method, target, version = parts
        if not all(self._is_token_byte(byte) for byte in method):
            self._fail(ParseErrorCode.MALFORMED_REQUEST_LINE, "invalid HTTP method")
        if not all(0x21 <= byte <= 0x7E for byte in target):
            self._fail(ParseErrorCode.MALFORMED_REQUEST_LINE, "invalid request target")
        if not self._valid_http_version(version):
            self._fail(ParseErrorCode.MALFORMED_REQUEST_LINE, "invalid HTTP version")

        self._method = method.decode("ascii")
        self._target = target.decode("ascii")
        self._version = version.decode("ascii")

    def _complete_header_line(self) -> None:
        line = bytes(self._line)
        if self._line_is_continuation:
            content = line.strip(b" \t").decode("latin-1")
            index = self._open_field_index
            if index is None:
                self._fail(
                    ParseErrorCode.MALFORMED_HEADER,
                    "continuation line without a preceding header",
                )
            name, value = self._fields[index]
            if content:
                value = f"{value} {content}" if value else content
            self._fields[index] = (name, value)
        else:
            name_bytes, value_bytes = line.split(b":", 1)
            name = name_bytes.decode("ascii").lower()
            value = value_bytes.strip(b" \t").decode("latin-1")
            self._fields.append((name, value))
            self._header_count += 1
            self._open_field_index = len(self._fields) - 1

        self._line.clear()
        self._line_is_continuation = False

    def _finish_request(self) -> None:
        headers: Dict[str, str] = {}
        for name, value in self._fields:
            if name in headers:
                headers[name] = f"{headers[name]}, {value}"
            else:
                headers[name] = value
        self._result = HttpRequest(
            method=self._method,
            target=self._target,
            version=self._version,
            headers=headers,
        )
        self._state = _State.DONE

    def _begin_continuation(self, byte: int) -> None:
        self._line.clear()
        self._append_header_byte(byte)

    def _append_header_byte(self, byte: int) -> None:
        self._current_header_size += 1
        if self._current_header_size > self.max_header_length:
            self._fail(
                ParseErrorCode.HEADER_TOO_LONG,
                f"header exceeds {self.max_header_length} bytes",
            )
        self._line.append(byte)

    def _fail(
        self,
        code: ParseErrorCode,
        message: str,
        *,
        offset: Optional[int] = None,
    ) -> None:
        error_offset = self._offset if offset is None else offset
        self._error = HttpParseError(code, message, error_offset)
        self._state = _State.ERROR
        raise self._error

    @staticmethod
    def _is_token_byte(byte: int) -> bool:
        return (
            0x30 <= byte <= 0x39
            or 0x41 <= byte <= 0x5A
            or 0x61 <= byte <= 0x7A
            or byte in _TOKEN_CHARS
        )

    @staticmethod
    def _valid_http_version(version: bytes) -> bool:
        if not version.startswith(b"HTTP/"):
            return False
        parts = version[5:].split(b".")
        return (
            len(parts) == 2
            and all(parts)
            and all(part.isdigit() for part in parts)
        )


def parse_request(data: bytes) -> HttpRequest:
    """Parse one complete request head supplied in a single bytes object."""
    parser = HttpRequestParser()
    result = parser.feed(data)
    if result is None:
        return parser.finish()
    return result
