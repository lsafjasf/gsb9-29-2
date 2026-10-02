"""Streaming multipart/form-data parser (Python standard library only).

Design notes
------------
* The caller feeds arbitrary byte chunks via :meth:`MultipartParser.feed`;
  completed parts are returned as :class:`Field` / :class:`FilePart` events.
* File parts are streamed straight to a temporary file on disk; only a
  small look-behind window (``len(delimiter) + 2`` bytes) plus the current
  read chunk is ever held in memory, so peak memory is independent of the
  uploaded file size.
* Boundary recognition follows RFC 2046: a delimiter is only recognised
  when ``CRLF "--" boundary`` is followed by ``CRLF`` (next part) or
  ``"--"`` (closing delimiter).  Any other trailing byte makes it content,
  so boundary-like prefixes inside file data never cause a false split.
  (A full ``CRLF--boundary CRLF`` sequence inside content *is* a delimiter
  per the RFC -- that is why real-world boundaries are chosen to not
  appear in the payload.)
"""

from __future__ import annotations

import os
import tempfile

from .errors import (
    FieldTooLarge,
    FileTooLarge,
    HeadersTooLarge,
    InvalidBoundary,
    MalformedForm,
    MultipartError,
    TooManyParts,
    TotalSizeExceeded,
    UnexpectedEOF,
)

# RFC 2046 boundary characters: DIGIT / ALPHA / '()+_,-./:=? / <space>
_BOUNDARY_EXTRA = b"'()+_,-./:=? "
_BOUNDARY_ALNUM = b"0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_BOUNDARY_ALLOWED = frozenset(_BOUNDARY_ALNUM + _BOUNDARY_EXTRA)

_CRLF = b"\r\n"
_HEADER_END = b"\r\n\r\n"


def validate_boundary(boundary):
    """Validate and normalise a boundary to ``bytes`` (RFC 2046 section 5.1.1)."""
    if isinstance(boundary, str):
        try:
            boundary = boundary.encode("ascii")
        except UnicodeEncodeError as exc:
            raise InvalidBoundary(
                "boundary must be ASCII, got %r" % (boundary,)
            ) from exc
    if not isinstance(boundary, (bytes, bytearray)):
        raise InvalidBoundary("boundary must be str or bytes")
    boundary = bytes(boundary)
    if not 1 <= len(boundary) <= 70:
        raise InvalidBoundary(
            "boundary length must be 1..70 bytes, got %d" % len(boundary),
            limit=70,
            got=len(boundary),
        )
    bad = sorted({c for c in boundary if c not in _BOUNDARY_ALLOWED})
    if bad:
        raise InvalidBoundary(
            "boundary contains illegal byte(s): %r" % bytes(bad)
        )
    if boundary.endswith(b" "):
        raise InvalidBoundary("boundary must not end with a space")
    return boundary


class Field:
    """A completed non-file form field (value kept in memory, bounded)."""

    kind = "field"
    __slots__ = ("name", "value", "headers")

    def __init__(self, name, value, headers):
        self.name = name
        self.value = value
        self.headers = headers

    @property
    def text(self):
        return self.value.decode("utf-8", "replace")

    def __repr__(self):
        return "Field(name=%r, %d bytes)" % (self.name, len(self.value))


class FilePart:
    """A completed file upload; content already flushed to ``path``."""

    kind = "file"
    __slots__ = ("name", "filename", "path", "size", "headers")

    def __init__(self, name, filename, path, size, headers):
        self.name = name
        self.filename = filename
        self.path = path
        self.size = size
        self.headers = headers

    @property
    def content_type(self):
        raw = self.headers.get("content-type", b"")
        return raw.decode("ascii", "replace")

    def __repr__(self):
        return "FilePart(name=%r, filename=%r, %d bytes)" % (
            self.name,
            self.filename,
            self.size,
        )


def _parse_header_params(value):
    """Parse ``form-data; name="a"; filename="b"`` into (main, params)."""
    segments = []
    buf = bytearray()
    in_quote = False
    escaped = False
    for byte in value:
        if escaped:
            buf.append(byte)
            escaped = False
        elif in_quote and byte == 0x5C:  # backslash escape inside quotes
            escaped = True
        elif byte == 0x22:  # '"'
            in_quote = not in_quote
            buf.append(byte)
        elif byte == 0x3B and not in_quote:  # ';'
            segments.append(bytes(buf))
            buf.clear()
        else:
            buf.append(byte)
    segments.append(bytes(buf))

    main = segments[0].strip().lower()
    params = {}
    for segment in segments[1:]:
        key, sep, val = segment.partition(b"=")
        if not sep:
            continue
        key = key.strip().lower().decode("ascii", "replace")
        val = val.strip()
        if len(val) >= 2 and val[:1] == b'"' and val[-1:] == b'"':
            val = val[1:-1]
        params[key] = val.decode("utf-8", "replace")
    return main, params


def _parse_headers(block):
    headers = {}
    if not block:
        return headers
    for line in block.split(_CRLF):
        if not line:
            continue
        if line[:1] in (b" ", b"\t"):
            raise MalformedForm("obsolete folded headers are not supported")
        name, sep, val = line.partition(b":")
        if not sep:
            raise MalformedForm("malformed header line: %r" % line[:64])
        name = name.strip().lower()
        if not name:
            raise MalformedForm("empty header name")
        headers[name.decode("ascii", "replace")] = val.strip()
    return headers


class MultipartParser:
    """Incremental multipart/form-data parser.

    Parameters
    ----------
    boundary:
        The boundary string from the Content-Type header.
    max_parts:
        Maximum number of parts allowed (default 1000).
    max_field_size:
        Maximum bytes for a single non-file field value (default 1 MiB).
    max_file_size:
        Maximum bytes for a single file part (default: unlimited, still
        bounded by ``max_total_size``).
    max_total_size:
        Maximum cumulative bytes fed to the parser (default 1 GiB).
    max_header_size:
        Maximum bytes of one part's header block (default 64 KiB).
    upload_dir:
        Directory for streamed file parts (default: system temp dir).
    """

    def __init__(
        self,
        boundary,
        *,
        max_parts=1000,
        max_field_size=1 << 20,
        max_file_size=None,
        max_total_size=1 << 30,
        max_header_size=64 * 1024,
        upload_dir=None,
        file_prefix="upload-",
    ):
        self.boundary = validate_boundary(boundary)
        self.max_parts = max_parts
        self.max_field_size = max_field_size
        self.max_file_size = max_file_size
        self.max_total_size = max_total_size
        self.max_header_size = max_header_size
        self.upload_dir = upload_dir
        self.file_prefix = file_prefix

        self._first = b"--" + self.boundary
        self._delim = _CRLF + b"--" + self.boundary
        self._buf = bytearray()
        self._state = "start"
        self._part_count = 0
        self._total = 0

        self._cur_headers = None
        self._cur_name = None
        self._cur_filename = None
        self._field_buf = None
        self._file = None
        self._file_path = None
        self._file_size = 0

    # ------------------------------------------------------------------ API

    def feed(self, data):
        """Feed a chunk of the request body; return completed parts."""
        if self._state == "error":
            raise MultipartError("parser is in a failed state")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("feed() expects bytes-like data")
        self._total += len(data)
        if self._total > self.max_total_size:
            self._fail()
            raise TotalSizeExceeded(
                "total body size exceeds %d bytes" % self.max_total_size,
                limit=self.max_total_size,
                got=self._total,
            )
        if self._state in ("done", "epilogue"):
            return []  # trailing epilogue bytes are ignored
        self._buf += bytes(data)
        try:
            return self._pump()
        except MultipartError:
            self._fail()
            raise

    def finish(self):
        """Signal end of input; return remaining completed parts."""
        if self._state == "error":
            raise MultipartError("parser is in a failed state")
        if self._state == "done":
            return []
        if self._state == "epilogue":
            self._state = "done"
            return []
        self._fail()
        raise UnexpectedEOF(
            "input ended in state %r; closing boundary not seen" % self._state
        )

    # ------------------------------------------------------------- internal

    def _fail(self):
        self._abort_file()
        self._state = "error"

    def _pump(self):
        out = []
        while True:
            state = self._state
            if state == "start":
                first = self._first
                if len(self._buf) < len(first) + 2:
                    return out
                if not self._buf.startswith(first):
                    raise MalformedForm(
                        "body does not start with the boundary delimiter"
                    )
                after = bytes(self._buf[len(first):len(first) + 2])
                if after == _CRLF:
                    del self._buf[:len(first) + 2]
                    self._begin_part()
                    self._state = "headers"
                elif after == b"--":
                    self._buf.clear()
                    self._state = "epilogue"
                    return out
                else:
                    raise MalformedForm(
                        "expected CRLF or '--' after boundary delimiter"
                    )
            elif state == "headers":
                idx = self._buf.find(_HEADER_END)
                if idx < 0:
                    if len(self._buf) > self.max_header_size:
                        raise HeadersTooLarge(
                            "part headers exceed %d bytes" % self.max_header_size,
                            limit=self.max_header_size,
                        )
                    return out
                if idx > self.max_header_size:
                    raise HeadersTooLarge(
                        "part headers exceed %d bytes" % self.max_header_size,
                        limit=self.max_header_size,
                        got=idx,
                    )
                block = bytes(self._buf[:idx])
                del self._buf[:idx + 4]
                self._start_body(_parse_headers(block))
            elif state == "body":
                delim = self._delim
                pos = self._buf.find(delim)
                if pos < 0:
                    # Keep a tail that may be a partial delimiter; everything
                    # before it is guaranteed delimiter-free content.
                    keep = len(delim) + 2
                    if len(self._buf) > keep:
                        self._write_body(bytes(self._buf[:-keep]))
                        del self._buf[:-keep]
                    return out
                if pos:
                    self._write_body(bytes(self._buf[:pos]))
                    del self._buf[:pos]
                if len(self._buf) < len(delim) + 2:
                    return out  # wait for the 2 decision bytes
                after = bytes(self._buf[len(delim):len(delim) + 2])
                if after == _CRLF:
                    del self._buf[:len(delim) + 2]
                    out.append(self._end_part())
                    self._begin_part()
                    self._state = "headers"
                elif after == b"--":
                    self._buf.clear()
                    out.append(self._end_part())
                    self._state = "epilogue"
                    return out
                else:
                    # False alarm: the delimiter-looking bytes are content.
                    # A real delimiter cannot start inside them because the
                    # boundary itself never contains CR/LF.
                    self._write_body(bytes(self._buf[:len(delim)]))
                    del self._buf[:len(delim)]
            else:  # "done" / "epilogue"
                self._buf.clear()
                return out

    def _begin_part(self):
        self._part_count += 1
        if self._part_count > self.max_parts:
            raise TooManyParts(
                "more than %d parts" % self.max_parts,
                limit=self.max_parts,
                got=self._part_count,
            )

    def _start_body(self, headers):
        disposition = headers.get("content-disposition")
        if disposition is None:
            raise MalformedForm("part is missing Content-Disposition")
        main, params = _parse_header_params(disposition)
        if main != b"form-data":
            raise MalformedForm(
                "unsupported Content-Disposition: %r" % disposition[:64]
            )
        name = params.get("name")
        if name is None:
            raise MalformedForm("part is missing a 'name' parameter")
        self._cur_headers = headers
        self._cur_name = name
        self._cur_filename = params.get("filename")
        if self._cur_filename is not None:
            fd, path = tempfile.mkstemp(
                prefix=self.file_prefix, dir=self.upload_dir
            )
            self._file = os.fdopen(fd, "wb")
            self._file_path = path
            self._file_size = 0
        else:
            self._field_buf = bytearray()
        self._state = "body"

    def _write_body(self, chunk):
        if not chunk:
            return
        if self._file is not None:
            self._file_size += len(chunk)
            if (
                self.max_file_size is not None
                and self._file_size > self.max_file_size
            ):
                raise FileTooLarge(
                    "file part exceeds %d bytes" % self.max_file_size,
                    limit=self.max_file_size,
                    got=self._file_size,
                )
            self._file.write(chunk)
        else:
            self._field_buf += chunk
            if len(self._field_buf) > self.max_field_size:
                raise FieldTooLarge(
                    "field %r exceeds %d bytes"
                    % (self._cur_name, self.max_field_size),
                    limit=self.max_field_size,
                    got=len(self._field_buf),
                )

    def _end_part(self):
        if self._file is not None:
            self._file.close()
            part = FilePart(
                self._cur_name,
                self._cur_filename,
                self._file_path,
                self._file_size,
                self._cur_headers,
            )
            self._file = None
            self._file_path = None
        else:
            part = Field(self._cur_name, bytes(self._field_buf), self._cur_headers)
            self._field_buf = None
        self._cur_headers = None
        return part

    def _abort_file(self):
        if self._file is not None:
            try:
                self._file.close()
            finally:
                self._file = None
        if self._file_path is not None:
            try:
                os.unlink(self._file_path)
            except OSError:
                pass
            self._file_path = None


def parse_multipart(stream, boundary, *, chunk_size=64 * 1024, **limits):
    """Yield parts from a file-like ``stream`` (e.g. a request body)."""
    parser = MultipartParser(boundary, **limits)
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        yield from parser.feed(chunk)
    yield from parser.finish()
