"""缺陷复现测试（确定性）。

这些测试针对 buggy_sync.BuggySynchronizer，断言三个缺陷确实发生，
作为修复前的稳定复现。修复后的对照断言见 test_time_sync.py。
"""
import random
import unittest

from buggy_sync import BuggySynchronizer
from simulator import Link, SimClock, run_scenario, true_offset


class ReproduceBugs(unittest.TestCase):
    def test_asymmetric_rtt_becomes_fixed_bias(self):
        # 上行 5ms + U(0,300ms) 拥塞抖动，下行 5ms + U(0,2ms)。
        # 无延迟筛选时，均值偏差 = (E[up]-E[down])/2 ≈ (155-6)/2 ≈ 74ms。
        local = SimClock()
        remote = SimClock(offset=1.0)
        link = Link(random.Random(7), up=0.005, down=0.005,
                    up_jitter=0.300, down_jitter=0.002)
        sync = BuggySynchronizer(window=8)
        recs = run_scenario(sync, local, remote, link,
                            duration=200.0, interval=1.0)
        bias = recs[-1]["estimate"] - 1.0
        self.assertGreater(abs(bias), 0.050)

    def test_long_run_drift_keeps_growing(self):
        # 远端频偏 3000ppm（3ms/s），同步间隔 2s，窗口 8：
        # 滑动平均对线性增长的滞后 = skew*(w-1)/2*interval = 3ms/s*7s = 21ms，
        # 且没有任何频偏补偿，滞后不会随时间收敛。
        local = SimClock()
        remote = SimClock(skew_ppm=3000.0)
        link = Link(random.Random(7), up=0.01, down=0.01, jitter=0.001)
        sync = BuggySynchronizer(window=8)
        recs = run_scenario(sync, local, remote, link,
                            duration=1800.0, interval=2.0)
        lag = [r["residual"] for r in recs if r["t"] >= 60.0]
        mean_lag = sum(lag) / len(lag)
        self.assertGreater(abs(mean_lag), 0.015)
        # 后段的滞后不比前段小：漂移没有被修正
        late = [abs(r["residual"]) for r in recs if r["t"] >= 1500.0]
        early = [abs(r["residual"]) for r in recs if 100.0 <= r["t"] < 400.0]
        self.assertGreaterEqual(sum(late) / len(late) * 0.9,
                                sum(early) / len(early))

    def test_clock_step_is_silently_eaten(self):
        # t=100s 远端时钟阶跃 +2s：没有任何事件记录，滑动窗口用整整一个
        # 窗口的样本把跳变慢慢平均掉，期间误差长期很大。
        local = SimClock()
        remote = SimClock(offset=0.5)
        link = Link(random.Random(7), up=0.01, down=0.01, jitter=0.001)
        sync = BuggySynchronizer(window=8)
        recs = run_scenario(sync, local, remote, link,
                            duration=200.0, interval=1.0,
                            steps=[(100.0, 2.0)])
        self.assertEqual(sync.events, [])  # 跳变无感知、无记录
        after = [r for r in recs if r["t"] >= 100.0]
        self.assertGreater(abs(after[0]["residual"]), 1.5)   # 几乎全幅误差
        self.assertGreater(abs(after[3]["residual"]), 0.5)   # 4 个样本后仍 >0.5s
        # 需要接近一个完整窗口的样本才"悄悄"恢复，且没有任何重新收敛信号
        converge_k = next(k for k, r in enumerate(after)
                          if abs(r["residual"]) < 0.05)
        self.assertGreaterEqual(converge_k, 6)

    def test_single_network_spike_pollutes_window(self):
        # 一个 0.8s 的下行排队尖峰直接把窗口均值带偏 ~50ms（尖峰/窗口）。
        local = SimClock()
        remote = SimClock(offset=0.5)
        link = Link(random.Random(7), up=0.01, down=0.01, jitter=0.0)
        sync = BuggySynchronizer(window=8)
        for k in range(8):
            t = float(k)
            sync.update(*link.exchange(t, local, remote))
        before = sync.estimate(8.0)
        # 手工注入一个下行尖峰样本
        sync.update(10.0, 10.51, 10.51, 10.81)
        after = sync.estimate(10.0)
        self.assertGreater(abs(after - before), 0.030)
        self.assertEqual(sync.events, [])


if __name__ == "__main__":
    unittest.main()
