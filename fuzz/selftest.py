#!/usr/bin/env python3
"""框架自测：可复现性、空样本、超大报文、深度极大、全部变异组合、
崩溃/超时检出与最小化。"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import Campaign, default_corpus, run_parser, classify
from message import (MAGIC, Field, Message, TAG_CONTAINER, TAG_INT, TAG_RAW,
                     TAG_STR, parse_lenient, serialize, serialize_with_layout)
from minimize import minimize
from mutators import STRUCT_MUTATORS, NestingMutator

PARSER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "parser_under_test.py")
TEST_TIMEOUT = 1.0


def make_campaign(**kw):
    kw.setdefault("do_minimize", False)
    kw.setdefault("timeout", TEST_TIMEOUT)
    return Campaign(**kw)


class TestReproducibility(unittest.TestCase):
    def test_same_seed_same_sequence_dry(self):
        c1 = make_campaign(seed=7, mode="struct", iterations=150)
        c2 = make_campaign(seed=7, mode="struct", iterations=150)
        r1 = c1.run(execute=False)
        r2 = c2.run(execute=False)
        self.assertEqual(r1["sequence_sha256"], r2["sequence_sha256"])

    def test_different_seed_different_sequence(self):
        c1 = make_campaign(seed=7, mode="struct", iterations=150)
        c2 = make_campaign(seed=8, mode="struct", iterations=150)
        self.assertNotEqual(c1.run(execute=False)["sequence_sha256"],
                            c2.run(execute=False)["sequence_sha256"])

    def test_same_seed_same_sequence_wet(self):
        c1 = make_campaign(seed=3, mode="struct", iterations=60)
        c2 = make_campaign(seed=3, mode="struct", iterations=60)
        r1 = c1.run()
        r2 = c2.run()
        self.assertEqual(r1["sequence_sha256"], r2["sequence_sha256"])
        self.assertEqual(r1["counts"], r2["counts"])


class TestEdgeCases(unittest.TestCase):
    def test_empty_sample(self):
        corpus = [Message([], flags=0)]
        c = make_campaign(seed=11, mode="struct", iterations=200,
                          corpus=corpus)
        report = c.run(execute=False)
        self.assertEqual(report["counts"]["generated"], 200)
        # 再生成一批直接验证头部结构完好
        for _ in range(50):
            data, _ = c.generate()
            self.assertEqual(data[:2], MAGIC)
            self.assertGreaterEqual(len(data), 6)

    def test_oversize_message_skipped(self):
        corpus = [Message([Field(TAG_RAW, b"\x00" * 16)], flags=0)]
        c = make_campaign(seed=5, mode="struct", iterations=300,
                          corpus=corpus, max_size=1024)
        report = c.run(execute=False)
        self.assertGreater(report["counts"]["oversize_skip"], 0)
        self.assertEqual(report["counts"]["generated"]
                         + report["counts"]["oversize_skip"], 300)

    def test_deep_nesting(self):
        rng = random.Random(1234)
        msg = Message([Field(TAG_INT, 1)], flags=0)
        mut = NestingMutator()
        for _ in range(200):
            mut.mutate(msg, rng)
            if msg.depth() > 300:
                break
        self.assertGreater(msg.depth(), 300)
        data = serialize(msg)
        back = parse_lenient(data)
        self.assertEqual(back.depth(), msg.depth())

    def test_all_mutation_combinations(self):
        seeds = default_corpus()
        for m1 in STRUCT_MUTATORS:
            for m2 in STRUCT_MUTATORS:
                for i, seed in enumerate(seeds):
                    rng = random.Random(hash((m1.name, m2.name, i)) & 0xFFFFFFFF)
                    msg = seed.clone()
                    for m in (m1, m2):
                        if not m.is_post:
                            m.mutate(msg, rng)
                    data, offsets = serialize_with_layout(msg.fields, msg.flags)
                    for m in (m1, m2):
                        if m.is_post:
                            data = m.mutate_bytes(data, offsets, rng)
                    self.assertIsInstance(data, bytes)
                    self.assertEqual(data[:2], MAGIC)


def crafted_length_crash():
    """篡改 STR 长度使其小 1，触发 BUG-1 的 IndexError。"""
    msg = Message([Field(TAG_INT, 42), Field(TAG_STR, b"hello")], flags=0x01)
    data, offsets = serialize_with_layout(msg.fields, msg.flags)
    str_len_off = offsets[2]
    buf = bytearray(data)
    buf[str_len_off:str_len_off + 2] = (4).to_bytes(2, "big")
    return bytes(buf)


class TestDetection(unittest.TestCase):
    def test_crash_detection(self):
        data = crafted_length_crash()
        rc, stderr, timed_out = run_parser(PARSER, False, data, TEST_TIMEOUT)
        category, sig = classify(rc, stderr, timed_out)
        self.assertEqual(category, "crash")
        self.assertEqual(sig, "IndexError")

    def test_timeout_detection(self):
        msg = Message([Field(TAG_RAW, b"\x00" * 0xFFF0)], flags=0x00)
        data = serialize(msg)
        rc, stderr, timed_out = run_parser(PARSER, False, data, TEST_TIMEOUT)
        category, sig = classify(rc, stderr, timed_out)
        self.assertEqual(category, "timeout")

    def test_hardened_parser_rejects_without_crash(self):
        data = crafted_length_crash()
        rc, stderr, timed_out = run_parser(PARSER, True, data, TEST_TIMEOUT)
        category, _ = classify(rc, stderr, timed_out)
        self.assertEqual(category, "reject_field")

    def test_valid_corpus_accepted(self):
        for seed_msg in default_corpus():
            data = serialize(seed_msg)
            rc, stderr, timed_out = run_parser(PARSER, False, data, TEST_TIMEOUT)
            category, _ = classify(rc, stderr, timed_out)
            self.assertEqual(category, "ok", msg=f"seed rejected: {stderr}")

    def test_minimizer_preserves_crash(self):
        data = crafted_length_crash()

        def pred(candidate):
            rc, stderr, timed_out = run_parser(PARSER, False, candidate,
                                               TEST_TIMEOUT)
            category, sig = classify(rc, stderr, timed_out)
            return sig == "IndexError"

        reduced = minimize(data, pred)
        self.assertLessEqual(len(reduced), len(data))
        self.assertTrue(pred(reduced))


class TestStructVsRandom(unittest.TestCase):
    def test_struct_reaches_deeper_and_detects(self):
        struct = make_campaign(seed=99, mode="struct", iterations=150)
        random_c = make_campaign(seed=99, mode="random", iterations=150)
        rs = struct.run()
        rr = random_c.run()
        self.assertGreater(rs["reached_field_stage_rate"],
                           rr["reached_field_stage_rate"])
        self.assertGreaterEqual(rs["detections"], 1)
        self.assertGreater(rs["max_depth_generated"], rr["max_depth_generated"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
