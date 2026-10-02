"""Differential tests vs the standard-library MIME parser (``email``).

The ``email`` package is an independent, battle-tested multipart
implementation.  We synthesise random multipart/form-data bodies (with
boundary-like junk planted inside the payload), parse them with both
implementations at various feed chunk sizes, and require identical
(name, filename, content) results.
"""

import os
import random
import sys
import tempfile
import unittest
from email.parser import BytesParser
from email.policy import default as default_policy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from multipart_stream import MultipartParser

BOUNDARY = b"----DiffBoundary9c3f81"

# Junk that both parsers agree is NOT a delimiter.  Deliberately excluded:
# "\r\n--B\rX" (lone CR), "\n--B" (lone LF) and "\r\n--B <SP>"
# (transport padding) -- the stdlib email parser is lenient there
# (universal newlines / padding) and splits, while this library stays
# strict to avoid corrupting uploaded files.  Those cases are covered by unit tests in test_parser.py instead.
NEAR_MISS_JUNK = [
    b"\r\n--" + BOUNDARY[:-1],            # one byte short
    b"\r\n--" + BOUNDARY + b"Z",         # full delimiter + junk byte
    b"\r\n--" + BOUNDARY + b"-",         # single dash only
    b"\r\n--" + BOUNDARY[:7],            # very short prefix
    b"\r\n--",
    b"\r\n-\r\n",
    b"\r\n\r\n--" + BOUNDARY,
]


def build_body(boundary, parts, closing=b"--\r\n"):
    out = bytearray()
    for headers, content in parts:
        out += b"--" + boundary + b"\r\n"
        for line in headers:
            out += line + b"\r\n"
        out += b"\r\n" + content + b"\r\n"
    out += b"--" + boundary + closing
    return bytes(out)


def reference_parse(boundary, body):
    raw = (
        b"Content-Type: multipart/form-data; boundary="
        + boundary
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + body
    )
    msg = BytesParser(policy=default_policy).parsebytes(raw)
    result = []
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        result.append((name, filename, payload))
    return result


def streaming_parse(boundary, body, chunk_size, upload_dir):
    parser = MultipartParser(boundary, upload_dir=upload_dir)
    events = []
    for i in range(0, len(body), chunk_size):
        events.extend(parser.feed(body[i:i + chunk_size]))
    events.extend(parser.finish())
    result = []
    for part in events:
        if part.kind == "file":
            with open(part.path, "rb") as fh:
                payload = fh.read()
            os.unlink(part.path)
        else:
            payload = part.value
        result.append((part.name, getattr(part, "filename", None), payload))
    return result


def random_content(rng, length):
    alphabet = b"abc\r\n---__" + BOUNDARY[:8]
    data = bytearray(rng.getrandbits(8) for _ in range(length))
    for i in range(length):
        if rng.random() < 0.5:
            data[i] = alphabet[rng.randrange(len(alphabet))]
    if rng.random() < 0.8:
        data.extend(rng.choice(NEAR_MISS_JUNK))
        data.extend(rng.choice(NEAR_MISS_JUNK))
    # A genuine delimiter-looking sequence is intrinsically ambiguous
    # (both parsers must split there), so reject it and regenerate.
    data = bytes(data)
    if (b"\r\n--" + BOUNDARY + b"\r\n") in data or (
        b"\r\n--" + BOUNDARY + b"--") in data:
        return random_content(rng, length)
    if data.endswith(b"\r\n--" + BOUNDARY):
        return random_content(rng, length)
    return data


def random_form(rng):
    parts = []
    for _ in range(rng.randrange(0, 7)):
        name = rng.choice(["a", "b b", "x-y", "_z", "中文"])
        content = random_content(rng, rng.randrange(0, 400))
        def quoted(text):
            return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')

        if rng.random() < 0.5:
            headers = [
                ("Content-Disposition: form-data; name=%s; filename=%s"
                 % (quoted(name), quoted(rng.choice(["f.bin", "a b.txt", "x;y"]))))
                .encode()
            ]
        else:
            headers = [
                ("Content-Disposition: form-data; name=%s" % quoted(name)).encode()
            ]
        parts.append((headers, content))
    return parts


class TestDifferential(unittest.TestCase):
    def test_many_random_forms_and_chunk_sizes(self):
        rng = random.Random(20261002)
        upload_dir = tempfile.mkdtemp()
        cases = 300
        for case in range(cases):
            parts = random_form(rng)
            body = build_body(BOUNDARY, parts)
            expected = reference_parse(BOUNDARY, body)
            for chunk_size in (1, 5, 13, 64, 4096, len(body) or 1):
                got = streaming_parse(BOUNDARY, body, chunk_size, upload_dir)
                self.assertEqual(
                    got, expected,
                    msg="case=%d chunk_size=%d" % (case, chunk_size),
                )

    def test_deterministic_empty_and_single(self):
        upload_dir = tempfile.mkdtemp()
        for body, count in [
            (b"--" + BOUNDARY + b"--\r\n", 0),
            (b"--" + BOUNDARY + b"--", 0),
        ]:
            got = streaming_parse(BOUNDARY, body, 1, upload_dir)
            self.assertEqual(len(got), count)
            self.assertEqual(got, reference_parse(BOUNDARY, body))


if __name__ == "__main__":
    unittest.main()
