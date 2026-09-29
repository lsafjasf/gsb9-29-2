"""http_parser 的自测：边界用例 + 与参照实现（标准库 http.client）对拍。

运行：python3 -m unittest -v test_http_parser
"""

import http.client
import io
import random
import unittest

from http_parser import (
    BadContinuation,
    EmptyRequest,
    HeaderTooLong,
    HTTPRequestParser,
    MalformedHeader,
    MalformedRequestLine,
    ParseError,
    RequestLineTooLong,
    TooManyHeaders,
    UnexpectedEOF,
    parse_request,
)


# ---------------------------------------------------------------- 参照实现

def _norm(value: str) -> str:
    """空白归一化：折叠所有空白游程为单空格并去首尾。"""
    return " ".join(value.split())


def reference_parse(data: bytes):
    """用标准库 http.client.parse_headers 解析，作为对拍参照。

    返回 (method, target, version, [(name, value), ...])，
    报头按与被测实现相同的规则合并。
    """
    head, sep, _ = data.partition(b"\r\n\r\n")
    assert sep, "reference: missing header terminator"
    lines = head.split(b"\r\n")
    method_b, target_b, version_b = lines[0].split(b" ")
    major, minor = version_b[len(b"HTTP/"):].split(b".")
    msg = http.client.parse_headers(
        io.BytesIO(b"\r\n".join(lines[1:]) + b"\r\n\r\n")
    )
    first_case = {}
    order = []
    for key in msg.keys():
        lkey = key.lower()
        if lkey not in first_case:
            first_case[lkey] = key
            order.append(lkey)
    headers = []
    for lkey in order:
        vals = [_norm(v) for v in msg.get_all(first_case[lkey])]
        joiner = "; " if lkey == "cookie" else ", "
        headers.append((first_case[lkey], joiner.join(vals)))
    return (
        method_b.decode("ascii"),
        target_b.decode("latin-1"),
        (int(major), int(minor)),
        headers,
    )


def as_tuple(req):
    return (
        req.method,
        req.target,
        req.version,
        [(name, _norm(value)) for name, value in req.headers],
    )


# ---------------------------------------------------------------- 边界用例

class EdgeCaseTests(unittest.TestCase):
    def test_simple_get(self):
        req = parse_request(b"GET /index.html HTTP/1.1\r\nHost: example.com\r\n\r\n")
        self.assertEqual(req.method, "GET")
        self.assertEqual(req.target, "/index.html")
        self.assertEqual(req.version, (1, 1))
        self.assertEqual(req.headers, [("Host", "example.com")])

    def test_empty_input(self):
        with self.assertRaises(EmptyRequest):
            parse_request(b"")

    def test_empty_request_line(self):
        with self.assertRaises(EmptyRequest):
            parse_request(b"\r\nHost: a\r\n\r\n")

    def test_only_method(self):
        with self.assertRaises(MalformedRequestLine):
            parse_request(b"GET\r\n\r\n")

    def test_method_and_target_only(self):
        with self.assertRaises(MalformedRequestLine):
            parse_request(b"GET / HTTP\r\n\r\n")

    def test_extra_spaces_in_request_line(self):
        with self.assertRaises(MalformedRequestLine):
            parse_request(b"GET  / HTTP/1.1\r\n\r\n")

    def test_bad_version(self):
        for bad in (b"GET / HTTP/1.\r\n\r\n",
                    b"GET / HTTP/.1\r\n\r\n",
                    b"GET / HTTP/1\r\n\r\n",
                    b"GET / HTTX/1.1\r\n\r\n"):
            with self.assertRaises(MalformedRequestLine, msg=bad):
                parse_request(bad)

    def test_request_line_too_long(self):
        data = b"GET /" + b"a" * 100 + b" HTTP/1.1\r\n\r\n"
        with self.assertRaises(RequestLineTooLong):
            parse_request(data, max_request_line=16)
        # 错误类型可区分：不是泛泛的 ParseError 子类混用
        with self.assertRaises(ParseError):
            parse_request(data, max_request_line=16)

    def test_header_too_long(self):
        data = b"GET / HTTP/1.1\r\nX-Big: " + b"A" * 100 + b"\r\n\r\n"
        with self.assertRaises(HeaderTooLong):
            parse_request(data, max_header_line=32)

    def test_header_too_long_via_continuation(self):
        data = (b"GET / HTTP/1.1\r\nX-Big: " + b"A" * 20 + b"\r\n"
                + b" " + b"B" * 20 + b"\r\n\r\n")
        with self.assertRaises(HeaderTooLong):
            parse_request(data, max_header_line=32)

    def test_too_many_headers(self):
        data = b"GET / HTTP/1.1\r\n" + b"".join(
            b"X-H%d: v\r\n" % i for i in range(10)
        ) + b"\r\n"
        with self.assertRaises(TooManyHeaders):
            parse_request(data, max_headers=5)

    def test_mixed_case_header_names(self):
        req = parse_request(
            b"GET / HTTP/1.1\r\n"
            b"cOnTeNt-TyPe: text/html\r\n"
            b"CONTENT-LENGTH: 5\r\n\r\n"
        )
        self.assertEqual(req.get("content-type"), "text/html")
        self.assertEqual(req.get("Content-Type"), "text/html")
        self.assertEqual(req.get("content-length"), "5")
        self.assertEqual(req.headers[0][0], "cOnTeNt-TyPe")  # 保留原始大小写

    def test_duplicate_headers_merged_with_comma(self):
        req = parse_request(
            b"GET / HTTP/1.1\r\n"
            b"Accept: text/html\r\n"
            b"ACCEPT: application/json\r\n"
            b"Accept: text/plain\r\n\r\n"
        )
        self.assertEqual(req.headers, [("Accept", "text/html, application/json, text/plain")])

    def test_cookie_merged_with_semicolon(self):
        req = parse_request(
            b"GET / HTTP/1.1\r\n"
            b"Cookie: a=1\r\n"
            b"Cookie: b=2\r\n\r\n"
        )
        self.assertEqual(req.get("cookie"), "a=1; b=2")

    def test_set_cookie_never_merged(self):
        req = parse_request(
            b"GET / HTTP/1.1\r\n"
            b"Set-Cookie: a=1\r\n"
            b"Set-Cookie: b=2\r\n\r\n"
        )
        self.assertEqual(req.get_all("set-cookie"), ["a=1", "b=2"])

    def test_continuation_line(self):
        req = parse_request(
            b"GET / HTTP/1.1\r\n"
            b"X-Folded: first\r\n"
            b"\tsecond\r\n"
            b"   third\r\n\r\n"
        )
        self.assertEqual(req.get("x-folded"), "first second third")

    def test_continuation_before_any_header(self):
        with self.assertRaises(BadContinuation):
            parse_request(b"GET / HTTP/1.1\r\n orphan\r\n\r\n")

    def test_header_missing_colon(self):
        with self.assertRaises(MalformedHeader):
            parse_request(b"GET / HTTP/1.1\r\nBadHeader\r\n\r\n")

    def test_invalid_header_name_byte(self):
        with self.assertRaises(MalformedHeader):
            parse_request(b"GET / HTTP/1.1\r\nBad Name: v\r\n\r\n")

    def test_bare_cr_rejected(self):
        with self.assertRaises(MalformedRequestLine):
            parse_request(b"GET / HTTP/1.1\rX\r\n\r\n")
        with self.assertRaises(MalformedHeader):
            parse_request(b"GET / HTTP/1.1\r\nA: b\rc\r\n\r\n")

    def test_unexpected_eof(self):
        with self.assertRaises(UnexpectedEOF):
            parse_request(b"GET / HTTP/1.1\r\nHost: a\r\n")

    def test_incremental_feed_matches_one_shot(self):
        data = (b"POST /submit?q=1 HTTP/1.0\r\nHost: h\r\n"
                b"X-A: 1\r\n x\r\nX-A: 2\r\nCookie: a=1\r\nCookie: b=2\r\n\r\n")
        one_shot = parse_request(data)
        for chunk in (1, 3, 7):
            parser = HTTPRequestParser()
            for i in range(0, len(data), chunk):
                parser.feed(data[i:i + chunk])
            self.assertEqual(as_tuple(parser.finish()), as_tuple(one_shot))

    def test_stops_reading_on_limit(self):
        # 越界立即抛错：后面的字节（包括非法字节）不会再被读取
        data = b"GET / HTTP/1.1\r\nX: " + b"A" * 64 + b"\xff\xfe\r\n\r\n"
        with self.assertRaises(HeaderTooLong):
            parse_request(data, max_header_line=16)

    def test_no_headers(self):
        req = parse_request(b"HEAD / HTTP/1.0\r\n\r\n")
        self.assertEqual(req.headers, [])


# ---------------------------------------------------------------- 对拍

class DifferentialTests(unittest.TestCase):
    CASES = [
        b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n",
        b"POST /submit HTTP/1.0\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: 0\r\n\r\n",
        b"GET /a/b?x=1&y=2 HTTP/1.1\r\nHost: h\r\nAccept: text/html\r\nAccept: */*\r\n\r\n",
        b"GET / HTTP/1.1\r\nx-mIxEd: v1\r\nX-Mixed: v2\r\n\r\n",
        b"GET / HTTP/1.1\r\nX-Fold: aaa\r\n bbb\r\n\tccc\r\n\r\n",
        b"GET / HTTP/1.1\r\nCookie: a=1\r\nCookie: b=2\r\nX: 1\r\n\r\n",
        b"OPTIONS * HTTP/1.1\r\n\r\n",
        b"GET / HTTP/1.1\r\nX-Empty:\r\nX-Spaces:    \r\n\r\n",
    ]

    def test_deterministic_cases(self):
        for data in self.CASES:
            with self.subTest(data=data):
                self.assertEqual(as_tuple(parse_request(data)), reference_parse(data))

    def test_fuzz_against_reference(self):
        rng = random.Random(20260930)
        names = [b"Host", b"Accept", b"Content-Type", b"X-Custom-Header",
                 b"Cookie", b"User-Agent", b"Accept-Encoding", b"Cache-Control",
                 b"X-Forwarded-For", b"Referer"]
        words = [b"alpha", b"beta", b"gamma", b"text/html", b"a=1", b"b=2",
                 b"gzip", b"deflate", b"keep-alive", b"123", b"*/", b"utf-8"]
        methods = [b"GET", b"POST", b"PUT", b"DELETE", b"HEAD", b"OPTIONS", b"PATCH"]

        for trial in range(400):
            method = rng.choice(methods)
            target = b"/" + b"/".join(rng.choice(words) for _ in range(rng.randint(0, 3)))
            version = rng.choice([b"HTTP/1.0", b"HTTP/1.1"])
            lines = [method + b" " + target + b" " + version]
            for _ in range(rng.randint(0, 12)):
                name = rng.choice(names)
                # 随机大小写混用
                name = bytes(c ^ 0x20 if rng.random() < 0.4 and chr(c).isalpha() else c
                             for c in name)
                value = b" ".join(rng.choice(words) for _ in range(rng.randint(1, 4)))
                line = name + b":" + b" " * rng.randint(0, 2) + value
                if rng.random() < 0.2:  # 续行（折叠为单空格）
                    cut = rng.randint(1, len(value) - 1)
                    line = name + b": " + value[:cut] + b"\r\n " + value[cut:].lstrip()
                lines.append(line)
            data = b"\r\n".join(lines) + b"\r\n\r\n"
            with self.subTest(trial=trial, data=data):
                self.assertEqual(as_tuple(parse_request(data)), reference_parse(data))

    def test_fuzz_chunked_feed(self):
        # 随机分块喂入，结果必须与一次性解析一致
        rng = random.Random(7)
        data = (b"GET /p HTTP/1.1\r\nHost: h\r\nX-A: 1\r\n 2\r\n"
                b"X-A: 3\r\nCookie: a=1\r\nCookie: b=2\r\n\r\n")
        expected = as_tuple(parse_request(data))
        for _ in range(50):
            parser = HTTPRequestParser()
            pos = 0
            while pos < len(data):
                step = rng.randint(1, 9)
                parser.feed(data[pos:pos + step])
                pos += step
            self.assertEqual(as_tuple(parser.finish()), expected)


if __name__ == "__main__":
    unittest.main()
