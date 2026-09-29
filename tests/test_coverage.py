"""Transition coverage: the corpus must exercise 100% of the defined
(state, event) transitions, and undefined combinations must raise."""

import unittest

from helpers import hand_crafted_streams, random_streams
from fsm_parser import (FsmParser, State, Event, UndefinedTransitionError,
                        defined_transitions, coverage_report)


def collect_hits():
    parser = FsmParser()
    for stream in hand_crafted_streams().values():
        parser.feed(stream)
    for stream in random_streams(count=300, seed=20260930):
        parser.feed(stream)
    return parser.transition_hits


class TestTransitionCoverage(unittest.TestCase):
    def test_full_coverage_of_defined_transitions(self):
        hits = collect_hits()
        missing = [k for k in defined_transitions() if not hits.get(k)]
        self.assertEqual([], missing,
                         "untested transitions: %r" % (missing,))

    def test_coverage_report_prints(self):
        hits = collect_hits()
        report, covered, total = coverage_report(hits)
        print("\n" + report)
        self.assertEqual(covered, total)

    def test_undefined_transitions_raise(self):
        # every (state, event) pair outside the table must raise, not be
        # silently tolerated
        parser = FsmParser()
        table = FsmParser._transitions()
        checked = 0
        for state in State:
            for event in Event:
                if (state, event) in table:
                    continue
                parser.state = state
                with self.assertRaises(UndefinedTransitionError):
                    parser._apply(state, event, 0x00)
                checked += 1
        self.assertEqual(checked, len(State) * len(Event) - len(table))

    def test_every_state_and_event_reachable_in_table(self):
        # sanity: table mentions every state and is total over its keys
        table = FsmParser._transitions()
        states_in_table = {s for s, _ in table} | {t for t, _ in table.values()}
        self.assertEqual(set(State), states_in_table)


if __name__ == "__main__":
    unittest.main()
