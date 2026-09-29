"""边界用例：单步管线、分支跳过、超大图、阶段失败回滚、缓冲池复用。"""

import random
import tracemalloc
import unittest

from legacy import process_image
from imgpipe import Pipeline, PipelineError
from imgpipe.codec import parse_header

from helpers import make_blob


class SingleStageTest(unittest.TestCase):
    def test_identity_pipeline_roundtrip(self):
        # 只走解码 + 编码，其余阶段全部跳过：输出应与输入逐字节一致
        rng = random.Random(11)
        pipe = Pipeline()
        for ch in (1, 3, 4):
            blob = make_blob(rng, 9, 7, ch, {"author": "a", "history": []})
            self.assertEqual(pipe.run(blob, {}), blob)

    def test_single_color_op(self):
        rng = random.Random(12)
        pipe = Pipeline()
        blob = make_blob(rng, 6, 6, 3)
        self.assertEqual(pipe.run(blob, {"invert": True}),
                         process_image(blob, {"invert": True}))


class BranchSkipTest(unittest.TestCase):
    def test_grayscale_skipped_on_non_rgb(self):
        # 1 通道图像请求灰度：分支应被跳过，history 中不出现 grayscale
        rng = random.Random(13)
        pipe = Pipeline()
        blob = make_blob(rng, 5, 5, 1)
        out = pipe.run(blob, {"grayscale": True})
        self.assertEqual(out, process_image(blob, {"grayscale": True}))
        self.assertNotIn("grayscale", parse_header(out)["meta"]["history"])

    def test_absent_ops_skip_stages(self):
        rng = random.Random(14)
        pipe = Pipeline()
        blob = make_blob(rng, 8, 6, 3)
        out = pipe.run(blob, {"flip_v": True})
        self.assertEqual(out, process_image(blob, {"flip_v": True}))
        history = parse_header(out)["meta"]["history"]
        self.assertEqual(history, ["flip_v"])  # 色彩/滤波等阶段均未触发


class HugeImageTest(unittest.TestCase):
    def test_huge_image_memory_bounded(self):
        # 4096x3072 RGB ≈ 36 MiB/缓冲；只跑行级操作避免纯 Python 逐像素循环过慢
        rng = random.Random(15)
        w, h, ch = 4096, 3072, 3
        raw = w * h * ch
        blob = make_blob(rng, w, h, ch)
        ops = {"flip_v": True}
        expected = process_image(blob, ops)

        pipe = Pipeline()
        tracemalloc.start()
        actual = pipe.run(blob, ops)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        self.assertEqual(actual, expected)  # 大图同样逐字节一致
        # 上界：blob + 输出 bytes 各 1 份，工作缓冲峰值 2 份，共 4*raw + 余量
        bound = 4 * raw + 4 * 1024 * 1024
        self.assertLess(peak, bound,
                        "peak=%d bound=%d" % (peak, bound))
        print("\n[huge] raw=%d peak=%d ratio=%.2f" % (raw, peak, peak / raw))


class RollbackTest(unittest.TestCase):
    def test_invalid_geometry_rolls_back(self):
        rng = random.Random(16)
        pipe = Pipeline()
        blob = make_blob(rng, 16, 16, 3, {"author": "x"})
        before = bytes(blob)

        with self.assertRaises(PipelineError) as ctx:
            pipe.run(blob, {"invert": True, "crop": [10, 10, 99, 99]})
        self.assertEqual(ctx.exception.stage, "geometry")
        self.assertIsInstance(ctx.exception.cause, ValueError)
        self.assertEqual(blob, before)              # 输入未被污染
        self.assertEqual(pipe.pool.checked_out, 0)  # 工作缓冲全部归还

        # 同一管线立即用合法参数重试，结果与 legacy 一致
        ok_ops = {"invert": True, "crop": [1, 1, 8, 8]}
        self.assertEqual(pipe.run(blob, ok_ops), process_image(blob, ok_ops))

    def test_injected_stage_failure_rolls_back(self):
        rng = random.Random(17)
        pipe = Pipeline()
        blob = make_blob(rng, 8, 8, 3)
        before = bytes(blob)

        class Boom:
            name = "boom"

            def apply(self, buf, ops, pool):
                raise RuntimeError("boom")

        pipe.stages.insert(1, Boom())  # 在色彩与几何之间注入失败阶段
        with self.assertRaises(PipelineError) as ctx:
            pipe.run(blob, {"invert": True, "flip_h": True})
        self.assertEqual(ctx.exception.stage, "boom")
        self.assertEqual(blob, before)
        self.assertEqual(pipe.pool.checked_out, 0)

        del pipe.stages[1]  # 移除故障阶段后管线恢复可用
        ops = {"invert": True, "flip_h": True}
        self.assertEqual(pipe.run(blob, ops), process_image(blob, ops))


class BufferPoolReuseTest(unittest.TestCase):
    def test_repeated_runs_allocate_nothing_new(self):
        rng = random.Random(18)
        pipe = Pipeline()
        blob = make_blob(rng, 64, 64, 3)
        ops = {"flip_h": True, "flip_v": True, "blur": 1}
        pipe.run(blob, ops)  # 第一次运行建立池
        allocations_after_first = pipe.pool.allocations
        self.assertGreater(allocations_after_first, 0)
        for _ in range(3):
            pipe.run(blob, ops)
        self.assertEqual(pipe.pool.allocations, allocations_after_first)
        self.assertGreater(pipe.pool.reuses, 0)
        self.assertEqual(pipe.pool.checked_out, 0)


if __name__ == "__main__":
    unittest.main()
