import io
import json
import types
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from http_parser import (
    MAX_HEADER_LENGTH,
    MAX_HEADERS,
    MAX_REQUEST_LINE_LENGTH,
    HttpParseError,
    HttpRequestParser,
    ParseErrorCode,
    parse_request,
)


CASES_PATH = Path(__file__).with_name("differential_cases.json")


def run_parser(data, chunks=None, parser_factory=HttpRequestParser):
    parser = parser_factory()
    try:
        if chunks is None:
            result = parser.feed(data)
        else:
            result = None
            for chunk in chunks:
                result = parser.feed(chunk)
        if result is None:
            result = parser.finish()
        return "ok", result
    except HttpParseError as error:
        return "error", error.code, error.offset


class ReferenceHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def __getattr__(self, name):
        if name.startswith("do_"):
            return lambda: None
        raise AttributeError(name)


def reference_parse(raw):
    handler = ReferenceHandler.__new__(ReferenceHandler)
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    handler.client_address = ("127.0.0.1", 12345)
    handler.server = types.SimpleNamespace(
        version_string=lambda: "test",
        date_time_string=lambda: "now",
    )
    handler.request_version = "HTTP/0.9"
    handler.handle_one_request()

    headers = {}
    for raw_name, raw_value in handler.headers.raw_items():
        name = raw_name.lower()
        lines = raw_value.replace("\r", "\n").split("\n")
        value = lines[0].strip(" \t")
        for continuation in lines[1:]:
            content = continuation.strip(" \t")
            if content:
                value = f"{value} {content}" if value else content
        headers[name] = f"{headers[name]}, {value}" if name in headers else value

    return types.SimpleNamespace(
        method=handler.command,
        target=handler.path,
        version=handler.request_version,
        headers=headers,
    )


class HttpRequestParserTests(unittest.TestCase):
    def test_basic_request_normalization(self):
        request = parse_request(
            b"GET /a HTTP/1.1\r\n"
            b"HOST: Example.COM\r\n"
            b"X-A: one\r\n"
            b"X-A: two\r\n"
            b"X-Fold: first\r\n"
            b"\t second  \r\n"
            b"X-Tab:\ta\tb\t\r\n"
            b"\r\n"
        )

        self.assertEqual(request.method, "GET")
        self.assertEqual(request.target, "/a")
        self.assertEqual(request.version, "HTTP/1.1")
        self.assertEqual(request.headers["host"], "Example.COM")
        self.assertEqual(request.headers["x-a"], "one, two")
        self.assertEqual(request.headers["x-fold"], "first second")
        self.assertEqual(request.headers["x-tab"], "a\tb")

    def test_every_two_part_split_matches_one_shot(self):
        cases = self._success_cases() + self._malformed_cases()
        for name, data in cases:
            with self.subTest(name=name):
                expected = run_parser(data)
                for split in range(1, len(data)):
                    actual = run_parser(data, [data[:split], data[split:]])
                    self.assertEqual(actual, expected)

    def test_one_byte_feeding_matches_one_shot(self):
        cases = self._success_cases() + self._malformed_cases()
        for name, data in cases:
            with self.subTest(name=name):
                expected = run_parser(data)
                chunks = [data[index : index + 1] for index in range(len(data))]
                self.assertEqual(run_parser(data, chunks), expected)

    def test_differential_against_standard_library(self):
        for item in self._json_cases():
            with self.subTest(name=item["name"]):
                raw = item["raw"].encode("ascii")
                request = parse_request(raw)
                reference = reference_parse(raw)

                self.assertEqual(request.method, reference.method)
                self.assertEqual(request.target, reference.target)
                self.assertEqual(request.version, reference.version)
                self.assertEqual(
                    list(request.headers.items()),
                    list(reference.headers.items()),
                )

    def test_empty_request_and_incomplete_request_line(self):
        self.assert_error_code(b"", ParseErrorCode.EMPTY_REQUEST)
        self.assert_error_code(b"\r\n", ParseErrorCode.MALFORMED_REQUEST_LINE)

        parser = HttpRequestParser()
        self.assertIsNone(parser.feed(b"GET"))
        with self.assertRaises(HttpParseError) as caught:
            parser.finish()
        self.assertEqual(caught.exception.code, ParseErrorCode.INCOMPLETE_REQUEST)

    def test_malformed_request_lines(self):
        self.assert_error_code(
            b"GET\r\n\r\n", ParseErrorCode.MALFORMED_REQUEST_LINE
        )
        self.assert_error_code(
            b"GET  / HTTP/1.1\r\n\r\n", ParseErrorCode.MALFORMED_REQUEST_LINE
        )
        self.assert_error_code(
            b"GET / FTP/1.1\r\n\r\n", ParseErrorCode.MALFORMED_REQUEST_LINE
        )
        self.assert_error_code(
            b"GET / HTTP/1.1 extra\r\n\r\n",
            ParseErrorCode.MALFORMED_REQUEST_LINE,
        )
        self.assert_error_code(
            b"GET / HTTP/1.1\nHost: a\n\n", ParseErrorCode.MALFORMED_REQUEST_LINE
        )

    def test_malformed_headers(self):
        self.assert_error_code(
            b"GET / HTTP/1.1\r\n bad: value\r\n\r\n",
            ParseErrorCode.MALFORMED_HEADER,
        )
        self.assert_error_code(
            b"GET / HTTP/1.1\r\nBad Name: value\r\n\r\n",
            ParseErrorCode.INVALID_CHARACTER,
        )
        self.assert_error_code(
            b"GET / HTTP/1.1\r\n: value\r\n\r\n",
            ParseErrorCode.INVALID_CHARACTER,
        )
        self.assert_error_code(
            b"GET / HTTP/1.1\r\nNo-Colon\r\n\r\n",
            ParseErrorCode.MALFORMED_HEADER,
        )
        self.assert_error_code(
            b"GET / HTTP/1.1\r\nX: bad\x00value\r\n\r\n",
            ParseErrorCode.INVALID_CHARACTER,
        )

    def test_request_line_boundary(self):
        fixed_prefix = len(b"GET /")
        fixed_suffix = len(b" HTTP/1.1")
        path_length = MAX_REQUEST_LINE_LENGTH - fixed_prefix - fixed_suffix
        exact = b"GET /" + b"a" * path_length + b" HTTP/1.1\r\n\r\n"
        self.assertEqual(parse_request(exact).method, "GET")

        too_long = b"GET /" + b"a" * (path_length + 1) + b" HTTP/1.1\r\n\r\n"
        self.assert_error_code(too_long, ParseErrorCode.REQUEST_LINE_TOO_LONG)

    def test_single_header_boundary(self):
        exact_value = MAX_HEADER_LENGTH - len(b"X: ")
        exact = (
            b"GET / HTTP/1.1\r\n"
            b"X: " + b"a" * exact_value + b"\r\n"
            b"\r\n"
        )
        self.assertEqual(
            parse_request(exact).headers["x"],
            "a" * exact_value,
        )

        too_long = (
            b"GET / HTTP/1.1\r\n"
            b"X: " + b"a" * (exact_value + 1) + b"\r\n"
            b"\r\n"
        )
        self.assert_error_code(too_long, ParseErrorCode.HEADER_TOO_LONG)

    def test_folded_header_boundary(self):
        first_line = b"X-Long: a"
        continuation_length = MAX_HEADER_LENGTH - len(first_line)
        continuation_content_length = continuation_length - 1
        exact = (
            b"GET / HTTP/1.1\r\n"
            + first_line
            + b"\r\n\t"
            + b"a" * continuation_content_length
            + b"\r\n\r\n"
        )
        self.assertEqual(
            parse_request(exact).headers["x-long"],
            "a " + "a" * continuation_content_length,
        )

        too_long = (
            b"GET / HTTP/1.1\r\n"
            + first_line
            + b"\r\n\t"
            + b"a" * (continuation_content_length + 1)
            + b"\r\n\r\n"
        )
        self.assert_error_code(too_long, ParseErrorCode.HEADER_TOO_LONG)

    def test_header_count_boundary(self):
        exact = b"GET / HTTP/1.1\r\n"
        exact += b"".join(f"X-{index}: v\r\n".encode() for index in range(MAX_HEADERS))
        exact += b"\r\n"
        self.assertEqual(len(parse_request(exact).headers), MAX_HEADERS)

        too_many = exact[:-2] + b"X-100: v\r\n\r\n"
        self.assert_error_code(too_many, ParseErrorCode.TOO_MANY_HEADERS)

    def test_parser_stops_reading_after_limit_error(self):
        parser = HttpRequestParser(max_request_line_length=3)
        with self.assertRaises(HttpParseError) as caught:
            parser.feed(b"GET /")
        first_error = caught.exception
        self.assertEqual(first_error.code, ParseErrorCode.REQUEST_LINE_TOO_LONG)
        self.assertTrue(parser.failed)

        offset_after_error = parser.offset
        with self.assertRaises(HttpParseError) as second:
            parser.feed(b"more input")
        self.assertEqual(second.exception.code, first_error.code)
        self.assertEqual(parser.offset, offset_after_error)

    def test_latin1_obs_text_is_preserved(self):
        request = parse_request(b"GET / HTTP/1.1\r\nX: caf\xe9\r\n\r\n")
        self.assertEqual(request.headers["x"], "café")

    def assert_error_code(self, data, expected_code):
        with self.assertRaises(HttpParseError) as caught:
            parse_request(data)
        self.assertEqual(caught.exception.code, expected_code)

    def _success_cases(self):
        return [
            (
                "mixed_case_and_duplicates",
                b"gEt /x?q=1 HTTP/1.1\r\n"
                b"HOST: A\r\n"
                b"X-Multi: one\r\n"
                b"X-Multi: two\r\n"
                b"\t folded\r\n"
                b"\r\n",
            ),
            ("no_headers", b"GET / HTTP/1.1\r\n\r\n"),
            ("empty_value", b"GET / HTTP/1.1\r\nX:\r\n\r\n"),
        ]

    def _malformed_cases(self):
        return [
            ("only_method", b"GET"),
            ("bare_lf", b"GET / HTTP/1.1\n\n"),
            ("bad_request_line", b"GET\r\n\r\n"),
            ("bad_header_name", b"GET / HTTP/1.1\r\nBad Name: x\r\n\r\n"),
            ("continuation_first", b"GET / HTTP/1.1\r\n x\r\n\r\n"),
            ("too_many_headers", b"GET / HTTP/1.1\r\n" + b"X: v\r\n" * 101),
        ]

    def test_custom_limit_errors_are_split_stable(self):
        cases = [
            (
                lambda: HttpRequestParser(max_request_line_length=8),
                b"GET /long HTTP/1.1\r\n\r\n",
            ),
            (
                lambda: HttpRequestParser(max_header_length=8),
                b"GET / HTTP/1.1\r\nX: 123456789\r\n\r\n",
            ),
            (
                lambda: HttpRequestParser(max_headers=1),
                b"GET / HTTP/1.1\r\nA: 1\r\nB: 2\r\n\r\n",
            ),
        ]
        for factory, raw in cases:
            expected = run_parser(raw, parser_factory=factory)
            for split in range(1, len(raw)):
                actual = run_parser(
                    raw,
                    [raw[:split], raw[split:]],
                    parser_factory=factory,
                )
                self.assertEqual(actual, expected)

    def _json_cases(self):
        return json.loads(CASES_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
