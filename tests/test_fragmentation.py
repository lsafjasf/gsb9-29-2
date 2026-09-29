"""Fragmentation tests: arbitrary chunkings of the same byte stream must
produce the identical event sequence and the identical final state."""

import itertools
import random
import unittest

from helpers import hand_crafted_streams, random_streams
from legacy_parser import LegacyParser
from fsm_parser import FsmParser


def feed_in_chunks(parser, stream, chunk_sizes):
    pos = 0
    for size in chunk_sizes:
        parser.feed(stream[pos:pos + size])
        pos += size
    if pos < len(stream):
        parser.feed(stream[pos:])
    return parser


def chunkings(stream, rng, max_random=200):
    """Yield representative chunkings of the stream."""
    n = len(stream)
    if n == 0:
        yield []
        return
    yield [n]                      # one shot
    yield [1] * n                  # byte by byte
    for cut in range(1, n):        # every two-chunk split
        yield [cut, n - cut]
    if n <= 16:                    # exhaustive: every composition
        for mask in range(1 << (n - 1)):
            sizes, run = [], 1
            for i in range(n - 1):
                if mask & (1 << i):
                    sizes.append(run)
                    run = 1
                else:
                    run += 1
            sizes.append(run)
            yield sizes
    else:                          # random chunkings for long streams
        for _ in range(max_random):
            sizes, left = [], n
            while left:
                size = min(left, rng.randrange(1, 9))
                sizes.append(size)
                left -= size
            yield sizes


class TestFragmentation(unittest.TestCase):
    def check_all_chunkings(self, stream, label, parser_cls):
        one_shot = parser_cls()
        one_shot.feed(stream)
        want_events = list(one_shot.events)
        want_state = getattr(one_shot, "state", None)
        rng = random.Random(hash((label, parser_cls.__name__)) & 0xFFFFFFFF)
        count = 0
        for sizes in chunkings(stream, rng):
            p = feed_in_chunks(parser_cls(), stream, sizes)
            self.assertEqual(want_events, list(p.events),
                             "%s/%s chunking=%r" % (parser_cls.__name__, label, sizes))
            if want_state is not None:
                self.assertEqual(want_state, p.state,
                                 "%s/%s final state, chunking=%r"
                                 % (parser_cls.__name__, label, sizes))
            count += 1
        return count

    def test_hand_crafted(self):
        total = 0
        for label, stream in hand_crafted_streams().items():
            with self.subTest(stream=label):
                total += self.check_all_chunkings(stream, label, FsmParser)
                total += self.check_all_chunkings(stream, label, LegacyParser)

    def test_random_streams(self):
        rng = random.Random(7)
        for i, stream in enumerate(random_streams(count=40, seed=991)):
            with self.subTest(random_index=i):
                self.check_all_chunkings(stream, "random#%d" % i, FsmParser)


if __name__ == "__main__":
    unittest.main()
