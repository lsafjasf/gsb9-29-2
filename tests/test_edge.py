"""边界用例：单步管线、分支跳过、超大图、阶段失败回滚、缓冲池上界。

运行：python3 -m unittest tests.test_edge -v   （或 python3 tests/test_edge.py）
超大图用例默认 2500x1600，可用环境变量 QUICK=1 缩小。
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from legacy import process_image
from pipeline import (BufferPool, ImageBuffer, Pipeline, PipelineError,
                      build_pipeline)
from pipeline.stages.decode import DecodeStage
from pipeline.stages.color import ColorStage
from pipeline.stages.geometry import GeometryStage
from pipeline.stages.filter import FilterStage
from pipeline.stages.encode import EncodeStage


def make_bmp(width, height, seed=7):
    rng = random.Random(seed)
    buf = ImageBuffer(bytearray(rng.randbytes(width * height * 3)),
                      width, height, 3, {"ops": []})
    bmp, _ = EncodeStage().run(buf, BufferPool())
    return bmp


class TestSingleStage(unittest.TestCase):
    """单步管线：只有解码+编码，应当是无损直通。"""

    def test_roundtrip_identity(self):
        for w, h in [(1, 1), (3, 3), (7, 5), (64, 48)]:
            bmp = make_bmp(w, h)
            out, meta = build_pipeline({}).run(bmp)
            self.assertEqual(out, bmp, "decode+encode 应当逐字节还原")
            self.assertEqual(meta, {"format": "BMP24", "width": w, "height": h,
                                    "channels": 3, "ops": []})

    def test_single_color_stage(self):
        bmp = make_bmp(9, 4)
        expected, _ = process_image(bmp, [{"name": "grayscale"}])
        out, _ = build_pipeline({"color": [{"name": "grayscale"}]}).run(bmp)
        self.assertEqual(out, expected)


class TestSkipStages(unittest.TestCase):
    """分支跳过：配置里缺省/为 None 的阶段被跳过，结果与 legacy 一致。"""

    def test_skip_each_stage(self):
        bmp = make_bmp(13, 11)
        variants = [
            ({"color": [{"name": "sepia"}]}, [{"name": "sepia"}]),
            ({"geometry": [{"name": "rotate90"}]}, [{"name": "rotate90"}]),
            ({"filter": [{"name": "boxblur"}]}, [{"name": "boxblur"}]),
            ({"color": [{"name": "invert"}], "filter": [{"name": "sharpen"}]},
             [{"name": "invert"}, {"name": "sharpen"}]),
            ({"color": None, "geometry": None, "filter": None}, []),
            ({"color": [], "geometry": [], "filter": []}, []),
        ]
        for config, legacy_ops in variants:
            expected, expected_meta = process_image(bmp, legacy_ops)
            out, meta = build_pipeline(config).run(bmp)
            self.assertEqual(out, expected, "config=%r" % (config,))
            self.assertEqual(meta, expected_meta, "config=%r" % (config,))


class TestHugeImage(unittest.TestCase):
    """超大图：与 legacy 逐字节一致，且缓冲池在途内存不超过 2 帧。"""

    def test_huge(self):
        quick = os.environ.get("QUICK")
        w, h = (800, 600) if quick else (2500, 1600)
        frame = w * h * 3
        bmp = make_bmp(w, h)
        ops = [{"name": "grayscale"}, {"name": "flip_h"}, {"name": "rotate90"}]
        config = {"color": ops[:1], "geometry": ops[1:]}

        pool = BufferPool()
        out, meta = build_pipeline(config, pool=pool).run(bmp)
        expected, expected_meta = process_image(bmp, ops)

        self.assertEqual(out, expected)
        self.assertEqual(meta, expected_meta)
        self.assertEqual(meta["width"], h)   # rotate90 后宽高互换
        self.assertEqual(meta["height"], w)
        # 内存上界：任意时刻在途缓冲 <= 2 帧；跑完后全部归还池中
        self.assertLessEqual(pool.max_live_bytes, 2 * frame)
        self.assertEqual(pool.live_bytes, 0)


class TestFailureRollback(unittest.TestCase):
    """阶段失败回滚：失败阶段的输入保持完好，缓冲池不泄漏。"""

    def setUp(self):
        self.bmp = make_bmp(21, 17)
        self.config = {"color": [{"name": "grayscale"}],
                       "filter": [{"name": "boxblur"}]}

    def _intermediate(self):
        """失败前最后一个完好状态：decode + grayscale 的输出。"""
        pipe = Pipeline([DecodeStage(), ColorStage([{"name": "grayscale"}])])
        return pipe.run(self.bmp)

    def test_rollback_checkpoint_intact(self):
        import pipeline.stages.filter as filt

        original = filt._OPS["boxblur"]

        def broken(buf, pool):
            out = pool.acquire(buf.nbytes)
            try:
                out[:24] = b"\xde\xad" * 12  # 模拟写了一半才失败
                raise RuntimeError("injected fault")
            except Exception:
                pool.release(out)
                raise

        filt._OPS["boxblur"] = broken
        pool = BufferPool()
        try:
            with self.assertRaises(PipelineError) as ctx:
                build_pipeline(self.config, pool=pool).run(self.bmp)
        finally:
            filt._OPS["boxblur"] = original

        err = ctx.exception
        self.assertEqual(err.stage_name, "filter")
        good = self._intermediate()
        ckpt = err.checkpoint
        self.assertIsInstance(ckpt, ImageBuffer)
        self.assertEqual(ckpt.width, good.width)
        self.assertEqual(ckpt.height, good.height)
        n = good.nbytes
        self.assertEqual(bytes(ckpt.data[:n]), bytes(good.data[:n]),
                         "checkpoint 必须是失败前的完好像素")
        self.assertEqual(ckpt.metadata["ops"], ["grayscale"])
        # checkpoint 由错误对象持有（有意保留）；消费完归还后池回到零在途
        pool.release(ckpt.data)
        self.assertEqual(pool.live_bytes, 0, "失败后缓冲不得泄漏")

    def test_rollback_allows_retry(self):
        """回滚后可用 checkpoint 继续走剩余阶段。"""
        class BoomStage:
            name = "boom"

            def run(self, item, pool):
                raise RuntimeError("boom")

        pipe = Pipeline([DecodeStage(),
                         ColorStage([{"name": "grayscale"}]),
                         BoomStage(),
                         EncodeStage()])
        with self.assertRaises(PipelineError) as ctx:
            pipe.run(self.bmp)
        # 从 checkpoint 重新接上后续阶段
        out, meta = Pipeline([EncodeStage()]).run(ctx.exception.checkpoint)
        expected, expected_meta = process_image(self.bmp, [{"name": "grayscale"}])
        self.assertEqual(out, expected)
        self.assertEqual(meta, expected_meta)

    def test_invalid_op_rejected_before_processing(self):
        with self.assertRaises(ValueError):
            build_pipeline({"color": [{"name": "no_such_op"}]})
        with self.assertRaises(ValueError):
            build_pipeline({"geometry": [{"name": "rotate37"}]})
        with self.assertRaises(ValueError):
            build_pipeline({"filter": [{"name": "gaussian"}]})

    def test_bad_input_fails_at_decode(self):
        with self.assertRaises(PipelineError) as ctx:
            build_pipeline({}).run(b"not a bmp at all")
        self.assertEqual(ctx.exception.stage_name, "decode")
        self.assertEqual(ctx.exception.checkpoint, b"not a bmp at all")


class TestBufferReuse(unittest.TestCase):
    """缓冲复用：阶段数增加，在途内存峰值不变。"""

    def test_peak_independent_of_stage_count(self):
        bmp = make_bmp(60, 40)
        frame = 60 * 40 * 3
        peaks = []
        for n_ops in (1, 4, 8):
            pool = BufferPool()
            config = {"color": [{"name": "invert"}] * n_ops,
                      "geometry": [{"name": "rotate90"}]}
            build_pipeline(config, pool=pool).run(bmp)
            peaks.append(pool.max_live_bytes)
        self.assertEqual(len(set(peaks)), 1, "峰值与阶段数无关: %r" % (peaks,))
        self.assertLessEqual(peaks[0], 2 * frame)


if __name__ == "__main__":
    unittest.main(verbosity=2)
