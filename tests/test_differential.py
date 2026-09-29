"""差分测试：同一字节流下，重构后 FSM 与遗留解析器的输出必须完全一致。"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fsm_parser import Parser
from legacy_parser import LegacyParser
from tests.util import directed_streams, random_stream


def run(cls, stream, chunks=None):
    parser = cls()
    if chunks is None:
        chunks = [stream]
    for chunk in chunks:
        parser.feed(chunk)
    parser.eof()
    return parser.events


class DifferentialTest(unittest.TestCase):
    def assert_equivalent(self, stream, chunks=None):
        legacy_events = run(LegacyParser, stream, chunks)
        fsm_events = run(Parser, stream, chunks)
        self.assertEqual(
            legacy_events, fsm_events,
            f"stream={stream!r} chunks={chunks!r}\nlegacy={legacy_events}\nfsm={fsm_events}",
        )

    def test_directed_streams(self):
        for stream in directed_streams():
            with self.subTest(stream=stream[:32]):
                self.assert_equivalent(stream)

    def test_random_streams_whole_and_chunked(self):
        rng = random.Random(20260930)
        for i in range(300):
            stream = random_stream(rng)
            with self.subTest(case=i):
                self.assert_equivalent(stream)
                # 随机分片下两者仍须一致
                chunks, pos = [], 0
                while pos < len(stream):
                    step = rng.randrange(1, len(stream) - pos + 1)
                    chunks.append(stream[pos:pos + step])
                    pos += step
                self.assert_equivalent(stream, chunks or [b""])

    def test_exhaustive_short_streams(self):
        # 穷举 4 字节内的关键前缀空间（magic/type/len 组合）
        rng = random.Random(7)
        for i in range(2000):
            stream = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 9)))
            with self.subTest(case=i, stream=stream):
                self.assert_equivalent(stream)


if __name__ == "__main__":
    unittest.main()
