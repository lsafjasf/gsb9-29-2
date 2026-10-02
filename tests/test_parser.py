"""Unit tests: framing, headers, edge cases, limits, boundary look-alikes."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from multipart_stream import (
    Field,
    FieldTooLarge,
    FilePart,
    FileTooLarge,
    HeadersTooLarge,
    InvalidBoundary,
    MalformedForm,
    MultipartParser,
    TooManyParts,
    TotalSizeExceeded,
    UnexpectedEOF,
)

BOUNDARY = b"----TestBoundary7d1a9f3"


def build_body(boundary, parts, closing=b"--\r\n"):
    """parts: list of (header_lines: list[bytes], content: bytes)."""
    out = bytearray()
    for header_lines, content in parts:
        out += b"--" + boundary + b"\r\n"
        for line in header_lines:
            out += line + b"\r\n"
        out += b"\r\n" + content + b"\r\n"
    out += b"--" + boundary + closing
    return bytes(out)


def field_headers(name):
    return [b'Content-Disposition: form-data; name="%s"' % name]


def file_headers(name, filename, ctype=b"application/octet-stream"):
    return [
        b'Content-Disposition: form-data; name="%s"; filename="%s"'
        % (name, filename),
        b"Content-Type: " + ctype,
    ]


def parse_all(body, boundary=BOUNDARY, chunk_size=None, **kw):
    kw.setdefault("upload_dir", tempfile.mkdtemp())
    parser = MultipartParser(boundary, **kw)
    parts = []
    if chunk_size is None:
        chunk_size = len(body) or 1
    for i in range(0, len(body), chunk_size):
        parts.extend(parser.feed(body[i:i + chunk_size]))
    parts.extend(parser.finish())
    return parts


class TestBasicFraming(unittest.TestCase):
    def test_fields_only(self):
        body = build_body(BOUNDARY, [
            (field_headers(b"user"), b"alice"),
            (field_headers(b"tag"), b"x"),
            (field_headers(b"empty"), b""),
        ])
        parts = parse_all(body)
        self.assertEqual([p.name for p in parts], ["user", "tag", "empty"])
        self.assertEqual(parts[0].value, b"alice")
        self.assertEqual(parts[2].value, b"")
        self.assertTrue(all(isinstance(p, Field) for p in parts))

    def test_file_part_written_to_disk(self):
        content = os.urandom(100_000)
        body = build_body(BOUNDARY, [
            (file_headers(b"doc", b"a.bin"), content),
            (field_headers(b"note"), b"done"),
        ])
        parts = parse_all(body)
        self.assertIsInstance(parts[0], FilePart)
        self.assertEqual(parts[0].filename, "a.bin")
        self.assertEqual(parts[0].size, len(content))
        with open(parts[0].path, "rb") as fh:
            self.assertEqual(fh.read(), content)
        self.assertEqual(parts[1].value, b"done")
        os.unlink(parts[0].path)

    def test_empty_form(self):
        for closing in (b"--\r\n", b"--", b"--\r\njunk-epilogue"):
            parts = parse_all(b"--" + BOUNDARY + closing)
            self.assertEqual(parts, [])

    def test_empty_body_is_error(self):
        parser = MultipartParser(BOUNDARY)
        with self.assertRaises(UnexpectedEOF):
            parser.finish()

    def test_truncated_body_is_error(self):
        body = build_body(BOUNDARY, [(field_headers(b"a"), b"1")])
        parser = MultipartParser(BOUNDARY)
        parser.feed(body[:-10])  # cut off closing delimiter
        with self.assertRaises(UnexpectedEOF):
            parser.finish()

    def test_wrong_boundary_in_body(self):
        with self.assertRaises(MalformedForm):
            parse_all(b"--WrongBoundary\r\n\r\n\r\n--WrongBoundary--\r\n")

    def test_missing_content_disposition(self):
        body = build_body(BOUNDARY, [(b"X-Foo: bar".split(b"\n"), b"v")])
        with self.assertRaises(MalformedForm):
            parse_all(body)

    def test_missing_name_parameter(self):
        body = build_body(BOUNDARY, [([b"Content-Disposition: form-data"], b"v")])
        with self.assertRaises(MalformedForm):
            parse_all(body)

    def test_filename_never_used_as_path(self):
        body = build_body(BOUNDARY, [
            (file_headers(b"f", b"../../etc/passwd"), b"x"),
        ])
        upload_dir = tempfile.mkdtemp()
        parts = parse_all(body, upload_dir=upload_dir)
        self.assertEqual(parts[0].filename, "../../etc/passwd")  # metadata kept
        self.assertEqual(os.path.dirname(parts[0].path), upload_dir)
        self.assertNotIn("passwd", os.path.basename(parts[0].path))
        os.unlink(parts[0].path)

    def test_quoted_semicolon_and_escape_in_filename(self):
        body = build_body(BOUNDARY, [
            (file_headers(b"f", b'a;\\"b\\".txt'), b"data"),
        ])
        parts = parse_all(body)
        self.assertEqual(parts[0].filename, 'a;"b".txt')
        os.unlink(parts[0].path)


class TestBoundaryValidation(unittest.TestCase):
    def test_invalid_boundaries(self):
        for bad in (b"", b"x" * 71, b"has\rcrlf", b"has\ncrlf",
                    b"bang!", "雪boundary".encode("utf-8"), b"trailing "):
            with self.assertRaises(InvalidBoundary, msg=repr(bad)):
                MultipartParser(bad)

    def test_valid_boundaries(self):
        MultipartParser(b"x" * 70)
        MultipartParser(b"a b")          # inner space allowed
        MultipartParser("simple-string")  # str accepted
        MultipartParser(b"'()+_,-./:=?")


class TestLimits(unittest.TestCase):
    def test_too_many_parts(self):
        body = build_body(BOUNDARY, [
            (field_headers(b"a"), b"1"),
            (field_headers(b"b"), b"2"),
            (field_headers(b"c"), b"3"),
        ])
        with self.assertRaises(TooManyParts) as ctx:
            parse_all(body, max_parts=2)
        self.assertEqual(ctx.exception.code, "too_many_parts")

    def test_field_too_large(self):
        body = build_body(BOUNDARY, [(field_headers(b"a"), b"x" * 11)])
        with self.assertRaises(FieldTooLarge) as ctx:
            parse_all(body, max_field_size=10)
        self.assertEqual(ctx.exception.limit, 10)

    def test_file_too_large_and_partial_file_removed(self):
        upload_dir = tempfile.mkdtemp()
        body = build_body(BOUNDARY, [(file_headers(b"f", b"big"), b"y" * 100)])
        with self.assertRaises(FileTooLarge):
            parse_all(body, max_file_size=50, upload_dir=upload_dir)
        self.assertEqual(os.listdir(upload_dir), [])  # partial file cleaned up

    def test_total_size_exceeded(self):
        body = build_body(BOUNDARY, [(field_headers(b"a"), b"z" * 500)])
        with self.assertRaises(TotalSizeExceeded) as ctx:
            parse_all(body, max_total_size=100)
        self.assertEqual(ctx.exception.code, "total_size_exceeded")

    def test_headers_too_large(self):
        body = build_body(BOUNDARY, [
            ([b"Content-Disposition: form-data; name=\"a\"",
              b"X-Pad: " + b"p" * 100], b"v"),
        ])
        with self.assertRaises(HeadersTooLarge):
            parse_all(body, max_header_size=64)

    def test_error_state_is_sticky(self):
        parser = MultipartParser(BOUNDARY, max_field_size=1)
        body = build_body(BOUNDARY, [(field_headers(b"a"), b"toolong")])
        with self.assertRaises(FieldTooLarge):
            parser.feed(body)
        with self.assertRaises(Exception):
            parser.feed(b"more")


class TestBoundaryLookAlikes(unittest.TestCase):
    """Content containing boundary-like prefixes must never be split."""

    def _roundtrip(self, content, chunk_sizes=(1, 2, 3, 7, 64, 4096, None)):
        body = build_body(BOUNDARY, [
            (file_headers(b"f", b"d.bin"), content),
            (field_headers(b"tail"), b"end"),
        ])
        for size in chunk_sizes:
            parts = parse_all(body, chunk_size=size)
            with open(parts[0].path, "rb") as fh:
                got = fh.read()
            os.unlink(parts[0].path)
            self.assertEqual(got, content,
                             "content corrupted at chunk_size=%r" % (size,))
            self.assertEqual(parts[1].value, b"end")

    def test_near_miss_sequences(self):
        b = BOUNDARY
        content = b"".join([
            b"plain text\r\n",
            b"--" + b + b"X\r\n",          # delimiter + junk byte
            b"--" + b + b"-X\r\n",         # delimiter + '-' + junk
            b"--" + b + b"\rX\r\n",        # delimiter + CR + junk
            b"--" + b[:-1] + b"\r\n",      # one byte short
            b"--" + b[:5] + b"\r\n",       # short prefix
            b"--\r\n--\r\n",               # dashes only
            b"\r\n",                       # bare CRLF
            b"mid-line --" + b + b" tail\r\n",  # not at line start
            b"\r\n--" + b + b" ",          # delimiter + space (padding-like)
            b"\r\n--" + b + b"\tX\r\n",    # delimiter + tab + junk
            b"end-without-newline",
        ])
        self._roundtrip(content)

    def test_delimiter_split_across_chunks(self):
        b = BOUNDARY
        content = b"A" * 10 + b"\r\n--" + b + b"Z" + b"B" * 10
        body = build_body(b, [(file_headers(b"f", b"x"), content)])
        # Feed byte-by-byte: every possible split point is exercised.
        parts = parse_all(body, chunk_size=1)
        with open(parts[0].path, "rb") as fh:
            self.assertEqual(fh.read(), content)
        os.unlink(parts[0].path)

    def test_content_ending_with_crlf_and_dashes(self):
        for tail in (b"\r\n", b"\r\n-", b"\r\n--", b"\r\n--" + BOUNDARY[:3]):
            self._roundtrip(b"payload" + tail)

    def test_binary_content_with_crlf_runs(self):
        rng_content = bytes(
            (i * 31 + 7) % 256 for i in range(3000)
        ) + b"\r\n" * 50 + b"--" * 100
        self._roundtrip(rng_content)


if __name__ == "__main__":
    unittest.main()


class TestRealWorld(unittest.TestCase):
    def test_webkit_style_body(self):
        boundary = b"----WebKitFormBoundary8Kf2ZqM1aB"
        body = (
            b"------WebKitFormBoundary8Kf2ZqM1aB\r\n"
            b'Content-Disposition: form-data; name="title"\r\n\r\n'
            b"hello world\r\n"
            b"------WebKitFormBoundary8Kf2ZqM1aB\r\n"
            b'Content-Disposition: form-data; name="upload"; filename="t.txt"\r\n'
            b"Content-Type: text/plain\r\n\r\n"
            b"line1\r\nline2\r\n"
            b"------WebKitFormBoundary8Kf2ZqM1aB--\r\n"
        )
        # Browsers emit delimiters as "--" + boundary, i.e. 6 dashes here.
        configured = boundary
        parts = parse_all(body, boundary=configured, chunk_size=1)
        self.assertEqual(parts[0].value, b"hello world")
        self.assertEqual(parts[0].text, "hello world")
        with open(parts[1].path, "rb") as fh:
            self.assertEqual(fh.read(), b"line1\r\nline2")
        self.assertEqual(parts[1].content_type, "text/plain")
        os.unlink(parts[1].path)

    def test_epilogue_after_closing_delimiter(self):
        body = build_body(BOUNDARY, [(field_headers(b"a"), b"v")],
                          closing=b"--\r\nsome ignored epilogue bytes")
        for size in (1, 7, 64):
            parts = parse_all(body, chunk_size=size)
            self.assertEqual(parts[0].value, b"v")

    def test_mixed_files_and_fields(self):
        body = build_body(BOUNDARY, [
            (field_headers(b"a"), b"1"),
            (file_headers(b"f1", b"a"), b"aaa"),
            (field_headers(b"b"), b"2"),
            (file_headers(b"f2", b"b"), b"bbbb"),
        ])
        parts = parse_all(body, chunk_size=3)
        kinds = [(p.kind, p.name) for p in parts]
        self.assertEqual(kinds,
                         [("field", "a"), ("file", "f1"),
                          ("field", "b"), ("file", "f2")])
        for p in parts:
            if p.kind == "file":
                os.unlink(p.path)
