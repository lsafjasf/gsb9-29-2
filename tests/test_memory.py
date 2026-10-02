"""Peak-memory benchmark for a huge streamed upload.

Runs as a unittest (128 MiB upload, with hard memory assertions) and as a
standalone script (``python3 tests/test_memory.py``) printing a table and
a naive "whole body buffered" baseline for contrast.
"""

import hashlib
import os
import resource
import sys
import tempfile
import time
import tracemalloc
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from multipart_stream import parse_multipart

BOUNDARY = b"----MemoryBenchBoundary01"
CHUNK_SIZE = 64 * 1024

# Boundary look-alike junk planted inside the big file.
JUNK = (
    b"\r\n--" + BOUNDARY + b"Znot-real\r\n"
    b"\r\n--" + BOUNDARY[:-1] + b"\r\n"
    b"\r\n-\r\n--\r\n"
)


def _content_block():
    """1 MiB deterministic block containing boundary look-alike junk."""
    block = hashlib.sha256(b"memory-benchmark-seed").digest() * (1 << 15)
    assert len(block) == 1 << 20
    view = bytearray(block)
    for offset in (0, 1024, 512 * 1024):
        view[offset:offset + len(JUNK)] = JUNK
    return bytes(view)


def body_segments(file_size):
    head = (
        b"--" + BOUNDARY + b"\r\n"
        b'Content-Disposition: form-data; name="upload"; filename="big.bin"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n"
    )
    tail = b"\r\n--" + BOUNDARY + b"--\r\n"
    block = _content_block()
    full, rem = divmod(file_size, len(block))
    yield head
    for _ in range(full):
        yield block
    if rem:
        yield block[:rem]
    yield tail


class ChunkedStream:
    """File-like object serving a generator in read(n)-sized chunks."""

    def __init__(self, segments):
        self._segments = iter(segments)
        self._buf = b""

    def read(self, size=-1):
        while not self._buf:
            try:
                self._buf = next(self._segments)
            except StopIteration:
                return b""
        if size is None or size < 0 or size >= len(self._buf):
            out = self._buf
            self._buf = b""
            return out
        out = self._buf[:size]
        self._buf = self._buf[size:]
        return out


def expected_content_hash(file_size):
    block = _content_block()
    full, rem = divmod(file_size, len(block))
    digest = hashlib.sha256()
    digest.update(block * full)
    if rem:
        digest.update(block[:rem])
    return digest.hexdigest()


def run_streaming_parse(file_size, upload_dir):
    stream = ChunkedStream(body_segments(file_size))
    parts = []
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    tracemalloc.start()
    start = time.monotonic()
    for part in parse_multipart(
        stream, BOUNDARY, chunk_size=CHUNK_SIZE,
        upload_dir=upload_dir,
    ):
        parts.append(part)
    _, peak_py = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    elapsed = time.monotonic() - start
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return parts, peak_py, rss_after - rss_before, elapsed


def run_naive_parse(file_size):
    """Baseline: buffer the whole body in memory, like naive parsers do."""
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    tracemalloc.start()
    start = time.monotonic()
    body = b"".join(body_segments(file_size))
    touch = len(body)  # the 'parsed' object owns the whole body
    digest = hashlib.sha256(body).hexdigest()
    _, peak_py = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    elapsed = time.monotonic() - start
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    del body
    return touch, digest, peak_py, rss_after - rss_before, elapsed


class TestMemoryIsBounded(unittest.TestCase):
    def test_huge_file_constant_memory(self):
        file_size = 128 * 1024 * 1024
        upload_dir = tempfile.mkdtemp()
        parts, peak_py, rss_delta, elapsed = run_streaming_parse(
            file_size, upload_dir
        )
        self.assertEqual(len(parts), 1)
        part = parts[0]
        self.assertEqual(part.size, file_size)
        with open(part.path, "rb") as fh:
            on_disk = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(on_disk, expected_content_hash(file_size))
        # tracemalloc peak: parser buffers + ~64 KiB chunk, allowance 4 MiB
        self.assertLess(
            peak_py, 4 * 1024 * 1024,
            "Python peak allocations %d bytes for a %d-byte upload"
            % (peak_py, file_size),
        )
        # RSS delta allowance is generous and independent of file size.
        self.assertLess(rss_delta, 48 * 1024, "rss delta %d KiB" % rss_delta)
        os.unlink(part.path)
        print(
            "\n[128 MiB upload] python peak %.2f MiB | rss delta %.2f MiB "
            "| %.1fs"
            % (peak_py / 1048576.0, rss_delta / 1024.0, elapsed)
        )


def main():
    print("Streaming parser (chunk size %d KiB)" % (CHUNK_SIZE // 1024))
    print("%12s | %14s | %14s | %8s" % ("file size", "py peak", "rss delta", "time"))
    upload_dir = tempfile.mkdtemp()
    last_path = None
    for mib in (16, 64, 256):
        size = mib * 1024 * 1024
        parts, peak_py, rss_delta, elapsed = run_streaming_parse(size, upload_dir)
        last_path = parts[0].path
        assert parts[0].size == size
        print("%9d MiB | %11.2f MiB | %11.2f MiB | %7.2fs"
              % (mib, peak_py / 1048576.0, rss_delta / 1024.0, elapsed))
    os.unlink(last_path)

    print("\nNaive baseline (whole body buffered in memory)")
    print("%12s | %14s | %14s | %8s" % ("file size", "py peak", "rss delta", "time"))
    for mib in (16, 64):
        size = mib * 1024 * 1024
        _, _, peak_py, rss_delta, elapsed = run_naive_parse(size)
        print("%9d MiB | %11.2f MiB | %11.2f MiB | %7.2fs"
              % (mib, peak_py / 1048576.0, rss_delta / 1024.0, elapsed))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        unittest.main()
    main()
