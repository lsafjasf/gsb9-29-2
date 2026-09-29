"""差分对拍：重构后的 imgpipe 与重构前的 legacy 逐字节比较。

每个用例同时断言：
1. 输出 blob 完全相等（像素 + 头部字节）；
2. 解析出的元数据 dict 相等（更清晰的失败定位）。
"""

import random
import unittest

from legacy import process_image
from imgpipe import Pipeline, PipelineError
from imgpipe.codec import parse_header

from helpers import make_blob

SIZES = [(1, 1), (1, 5), (7, 1), (2, 2), (3, 5), (16, 16), (31, 17), (48, 64)]


def random_ops(rng, width, height):
    ops = {}
    if rng.random() < 0.5:
        ops["grayscale"] = True
    if rng.random() < 0.5:
        ops["brightness"] = rng.randint(-80, 80)
    if rng.random() < 0.4:
        ops["invert"] = True
    if rng.random() < 0.5:
        cw = rng.randint(1, width)
        hh = rng.randint(1, height)
        ops["crop"] = [rng.randint(0, width - cw), rng.randint(0, height - hh), cw, hh]
    if rng.random() < 0.4:
        ops["flip_h"] = True
    if rng.random() < 0.4:
        ops["flip_v"] = True
    if rng.random() < 0.3:
        ops["rotate90"] = True
    if rng.random() < 0.4:
        ops["blur"] = rng.randint(0, 2)
    return ops


class DifferentialTest(unittest.TestCase):
    def test_random_pipelines_match_legacy(self):
        rng = random.Random(20260930)
        pipe = Pipeline()  # 复用同一管线（含缓冲池）跑全部用例
        for case in range(240):
            w, h = SIZES[rng.randrange(len(SIZES))]
            ch = rng.choice([1, 3, 4])
            meta = {"author": "tester", "history": ["import"]} \
                if rng.random() < 0.5 else {}
            blob = make_blob(rng, w, h, ch, meta)
            ops = random_ops(rng, w, h)
            expected = process_image(blob, ops)
            actual = pipe.run(blob, ops)
            label = "case %d size=%sx%sx%s ops=%r" % (case, w, h, ch, ops)
            self.assertEqual(actual, expected, "pixel/header bytes differ: " + label)
            self.assertEqual(
                parse_header(actual)["meta"],
                parse_header(expected)["meta"],
                "metadata differs: " + label,
            )

    def test_every_single_op_matches_legacy(self):
        # 每个操作单独成例，确保逐例一致、无遗漏
        rng = random.Random(7)
        pipe = Pipeline()
        op_list = [
            {},
            {"grayscale": True},
            {"brightness": -50},
            {"brightness": 0},
            {"brightness": 70},
            {"invert": True},
            {"crop": [1, 0, 3, 4]},
            {"crop": [0, 0, 5, 4]},
            {"flip_h": True},
            {"flip_v": True},
            {"rotate90": True},
            {"blur": 0},
            {"blur": 1},
            {"blur": 2},
        ]
        for ch in (1, 3, 4):
            blob = make_blob(rng, 5, 4, ch, {"history": ["seed"]})
            for ops in op_list:
                expected = process_image(blob, ops)
                actual = pipe.run(blob, ops)
                self.assertEqual(actual, expected, "ch=%d ops=%r" % (ch, ops))

    def test_corrupt_inputs_rejected_like_legacy(self):
        rng = random.Random(1)
        pipe = Pipeline()
        good = make_blob(rng, 4, 4, 3)
        bad_blobs = [
            b"",                 # 太短
            b"XXXX........",     # magic 错误
            good[:10],           # 头部不完整
            good[:-5],           # 像素截断
        ]
        for bad in bad_blobs:
            with self.assertRaises(ValueError):
                process_image(bad, {})
            with self.assertRaises(PipelineError) as ctx:
                pipe.run(bad, {})
            self.assertEqual(ctx.exception.stage, "decode")
            self.assertIsInstance(ctx.exception.cause, ValueError)
            self.assertEqual(pipe.pool.checked_out, 0)  # 失败无缓冲泄漏


if __name__ == "__main__":
    unittest.main()
