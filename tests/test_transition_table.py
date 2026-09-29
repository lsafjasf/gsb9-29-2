"""迁移表结构测试：完整性、动作可解析、未定义组合显式报错。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fsm_parser import (
    STATE_EVENTS,
    TRANSITIONS,
    Event,
    Parser,
    State,
    UndefinedTransitionError,
)


class TransitionTableTest(unittest.TestCase):
    def test_table_exactly_covers_declared_events(self):
        expected = {(s, e) for s, events in STATE_EVENTS.items() for e in events}
        self.assertEqual(expected, set(TRANSITIONS),
                         "迁移表与 STATE_EVENTS 声明不一致（缺行或存在死行）")

    def test_every_state_declared(self):
        self.assertEqual(set(State), set(STATE_EVENTS))

    def test_actions_resolve(self):
        for (state, event), (next_state, action) in TRANSITIONS.items():
            self.assertIsInstance(next_state, State)
            self.assertTrue(callable(getattr(Parser, action, None)),
                            f"{state}/{event} -> 未定义的动作 {action}")

    def test_undefined_combination_raises(self):
        parser = Parser()  # WAIT_MAGIC
        with self.assertRaises(UndefinedTransitionError):
            parser._step(Event.KNOWN_TYPE, 0x01)

    def test_classifier_output_is_declared(self):
        # 分类器对任意字节只能产出 STATE_EVENTS 内的事件（_classify 内部强校验，
        # 这里直接抽样验证不抛异常且结果合法）
        parser = Parser()
        for byte in (0x00, 0xAA, 0xFF, 0x01):
            event = parser._classify(byte)
            self.assertIn(event, STATE_EVENTS[parser.state])


if __name__ == "__main__":
    unittest.main()
