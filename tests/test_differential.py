"""Differential tests: FsmParser must be behaviorally identical to the
frozen LegacyParser on identical byte streams -- same messages, same
errors, same error strings, in the same order."""

import unittest

from helpers import hand_crafted_streams, random_streams
from legacy_parser import LegacyParser
from fsm_parser import FsmParser


def run_both(stream):
    legacy = LegacyParser()
    fsm = FsmParser()
    legacy_events = list(legacy.feed(stream))
    fsm_events = list(fsm.feed(stream))
    return legacy_events, fsm_events


class TestDifferential(unittest.TestCase):
    def check_stream(self, stream, label):
        legacy_events, fsm_events = run_both(stream)
        self.assertEqual(
            legacy_events, fsm_events,
            "mismatch on %s\nlegacy: %r\nfsm:    %r"
            % (label, legacy_events, fsm_events))

    def test_hand_crafted(self):
        for label, stream in hand_crafted_streams().items():
            with self.subTest(stream=label):
                self.check_stream(stream, label)

    def test_random_streams(self):
        for i, stream in enumerate(random_streams(count=300, seed=20260930)):
            with self.subTest(random_index=i):
                self.check_stream(stream, "random#%d" % i)

    def test_error_strings_byte_identical(self):
        # equality of the event tuples already covers this, but keep an
        # explicit assertion on the rendered strings for clarity
        for label, stream in hand_crafted_streams().items():
            legacy_events, fsm_events = run_both(stream)
            self.assertEqual([repr(e) for e in legacy_events],
                             [repr(e) for e in fsm_events], label)


if __name__ == "__main__":
    unittest.main()
