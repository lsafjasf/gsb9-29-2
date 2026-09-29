"""分片不变性：任意切分方式喂入，迁移序列与输出必须完全一致。"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fsm_parser import Parser
from tests.util import all_chunkings, build_frame, directed_streams, random_chunkings


def run_fsm(chunks):
    parser = Parser()
    for chunk in chunks:
        parser.feed(chunk)
    parser.eof()
    return parser.events, parser.transitions


class FragmentationTest(unittest.TestCase):
    def check_invariant(self, stream, chunkings):
        ref_events, ref_transitions = run_fsm([stream])
        for chunks in chunkings:
            events, transitions = run_fsm(chunks)
            self.assertEqual(ref_events, events, f"chunks={chunks!r}")
            self.assertEqual(
                ref_transitions, transitions,
                f"迁移序列随切分改变: chunks={chunks!r}",
            )

    def test_exhaustive_chunkings_of_short_frame(self):
        # 13 字节帧（含 0xAA 负载）的全部 2^11 = 2048 种切分
        stream = build_frame(0x03, b"\xaa\x00\xffhello")
        self.assertEqual(len(stream), 12)
        self.check_invariant(stream, all_chunkings(stream))

    def test_exhaustive_chunkings_of_error_stream(self):
        # 错帧 + 恢复，共 11 字节，全部 2^10 = 1024 种切分
        stream = build_frame(0x02, b"xy", checksum_delta=0xFF) + build_frame(0x01, b"")
        self.check_invariant(stream, all_chunkings(stream))

    def test_random_chunkings_of_directed_streams(self):
        rng = random.Random(42)
        for stream in directed_streams():
            with self.subTest(stream=stream[:32]):
                self.check_invariant(stream, random_chunkings(rng, stream, 50))

    def test_every_chunk_size(self):
        rng = random.Random(99)
        stream = b"".join(
            build_frame(0x03, bytes(rng.randrange(256) for _ in range(40)), ext=True)
            for _ in range(3)
        )
        chunkings = (
            [stream[i:i + size] for i in range(0, len(stream), size)]
            for size in range(1, len(stream) + 1)
        )
        self.check_invariant(stream, chunkings)


if __name__ == "__main__":
    unittest.main()
