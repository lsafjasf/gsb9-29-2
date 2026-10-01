"""TOTP 库自测：标准向量对拍 + 边界 + 漂移 + 重放 + 存储上界 + 异常。

运行：python3 -m unittest -v   或   python3 test_totp.py
"""
import hashlib
import os
import tempfile
import unittest

from drift import DriftEstimator
from replay import ReplayStore
from totp import TOTP, counter_for, hotp
from verifier import Verifier, const_time_equal

KEY20 = b"12345678901234567890"          # RFC 4226 / 6238 SHA-1   种子
KEY32 = b"12345678901234567890123456789012"      # RFC 6238 SHA-256 种子
KEY64 = (b"12345678901234567890123456789012"
         b"34567890123456789012345678901234")    # RFC 6238 SHA-512 种子


class FakeClock:
    def __init__(self, t=0.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def set(self, t):
        self.t = float(t)

    def advance(self, dt):
        self.t += dt


def make_verifier(key=KEY20, clock=None, store=None, **kw):
    clock = clock or FakeClock(0)
    totp = TOTP(key, digits=6, period=30, clock=clock)
    store = store or ReplayStore()
    return Verifier(totp, store, user="alice", **kw), totp, clock, store


# ---------------------------------------------------------------- 标准向量

class TestRFCVectors(unittest.TestCase):
    """与 RFC 4226 / RFC 6238 官方测试向量逐位对拍。"""

    def test_rfc4226_hotp_6_digits(self):
        # RFC 4226 Appendix D
        expected = ["755224", "287082", "359152", "969429", "338314",
                    "254676", "287922", "162583", "399871", "520489"]
        for counter, code in enumerate(expected):
            self.assertEqual(hotp(KEY20, counter, digits=6), code,
                             "counter=%d" % counter)

    def test_rfc6238_8_digits_all_hashes(self):
        # RFC 6238 Appendix B：T0=0, period=30, 8 位
        vectors = {
            59:          ("94287082", "46119246", "90693936"),
            1111111109:  ("07081804", "68084774", "25091201"),
            1111111111:  ("14050471", "67062674", "99943326"),
            1234567890:  ("89005924", "91819424", "93441116"),
            2000000000:  ("69279037", "90698825", "38618901"),
            20000000000: ("65353130", "77737706", "47863826"),
        }
        keys = {hashlib.sha1: KEY20, hashlib.sha256: KEY32,
                hashlib.sha512: KEY64}
        for t, (exp1, exp256, exp512) in vectors.items():
            for digest, exp in ((hashlib.sha1, exp1),
                                (hashlib.sha256, exp256),
                                (hashlib.sha512, exp512)):
                totp = TOTP(keys[digest], digits=8, period=30, digest=digest)
                got = totp.at(t)
                self.assertEqual(got, exp,
                                 "t=%d digest=%s" % (t, digest().name))
                self.assertEqual(len(got), 8)

    def test_digit_widths_consistent(self):
        # 不同位数来自同一截断值：低位对齐
        for counter in range(20):
            c8 = hotp(KEY20, counter, digits=8)
            self.assertEqual(hotp(KEY20, counter, digits=6), c8[-6:])
            self.assertEqual(hotp(KEY20, counter, digits=7), c8[-7:])
            self.assertTrue(all(ch.isdigit() for ch in c8))


# ---------------------------------------------------------------- 边界行为

class TestBoundary(unittest.TestCase):
    """窗口切换瞬间：t == k*period 属于新窗口 k（floor 语义）。"""

    def test_counter_floor_semantics(self):
        self.assertEqual(counter_for(0, 30), 0)
        self.assertEqual(counter_for(29, 30), 0)
        self.assertEqual(counter_for(30, 30), 1)   # 边界归入新窗口
        self.assertEqual(counter_for(59, 30), 1)
        self.assertEqual(counter_for(60, 30), 2)
        self.assertEqual(counter_for(59.999999, 30), 1)

    def test_totp_at_boundary(self):
        totp = TOTP(KEY20, digits=6, period=30)
        self.assertEqual(totp.at(59), hotp(KEY20, 1))
        self.assertEqual(totp.at(60), hotp(KEY20, 2))
        self.assertNotEqual(totp.at(59), totp.at(60))

    def test_verify_at_switch_instant(self):
        # 严格校验（无容忍）：切换瞬间只认新窗口口令
        verifier, totp, clock, _ = make_verifier(
            back=0, forward=0, learn_back=0, learn_forward=0)
        old_code = totp.at(59)   # 窗口 1
        new_code = totp.at(60)   # 窗口 2
        self.assertFalse(verifier.verify(old_code, at_time=60))
        self.assertTrue(verifier.verify(new_code, at_time=60))

    def test_verify_old_window_within_tolerance(self):
        # back=1：切换瞬间旧窗口口令仍可用一次
        verifier, totp, clock, _ = make_verifier(
            back=1, forward=0, learn_back=1, learn_forward=0)
        old_code = totp.at(59)
        self.assertTrue(verifier.verify(old_code, at_time=60))

    def test_verify_next_window_with_forward(self):
        # forward=1：窗口结束前瞬间，下一窗口口令已可用
        verifier, totp, clock, _ = make_verifier(
            back=0, forward=1, learn_back=0, learn_forward=1)
        next_code = totp.at(60)
        self.assertTrue(verifier.verify(next_code, at_time=59.999))


# ---------------------------------------------------------------- 防重放

class TestReplay(unittest.TestCase):
    def test_same_code_twice_rejected(self):
        verifier, totp, clock, _ = make_verifier()
        code = totp.at(1000)
        self.assertTrue(verifier.verify(code, at_time=1000))
        self.assertFalse(verifier.verify(code, at_time=1000))  # 重放拒绝

    def test_replay_after_restart(self):
        # 持久化恢复：重启后旧口令仍被拒绝
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "replay.json")
            v1, totp, _, _ = make_verifier(store=ReplayStore(path=path))
            code = totp.at(1000)
            self.assertTrue(v1.verify(code, at_time=1000))
            # 模拟重启：全新 Verifier + 同路径存储
            v2, _, _, _ = make_verifier(store=ReplayStore(path=path))
            self.assertFalse(v2.verify(code, at_time=1000))

    def test_replay_after_time_jump_backward(self):
        # 时钟回拨后，未来窗口已用过的口令仍被拒绝
        verifier, totp, clock, _ = make_verifier()
        code = totp.at(1000)
        self.assertTrue(verifier.verify(code, at_time=1000))
        clock.set(940)  # 回拨两个窗口
        self.assertFalse(verifier.verify(code, at_time=940))

    def test_store_max_users_lru(self):
        store = ReplayStore(max_users=2, retention_windows=100)
        store.record("a", 1, 1)
        store.record("b", 1, 1)
        store.record("c", 1, 1)  # 超出限额，淘汰最久未活动的 a
        self.assertEqual(store.user_count(), 2)
        self.assertFalse(store.seen("a", 1))
        self.assertTrue(store.seen("c", 1))

    def test_store_per_user_hard_cap(self):
        store = ReplayStore(max_counters_per_user=4, retention_windows=1000)
        for c in range(10):
            store.record("u", c, 0)
        self.assertEqual(store.counter_count("u"), 4)  # 内存有硬上界
        self.assertTrue(store.seen("u", 9))
        self.assertFalse(store.seen("u", 0))           # 最旧的被淘汰

    def test_store_expiry_prune(self):
        store = ReplayStore(retention_windows=3)
        store.record("u", 5, 5)
        self.assertTrue(store.seen("u", 5))
        store.record("u", 20, 20)  # 窗口 5 已过期，被回收
        self.assertFalse(store.seen("u", 5))
        self.assertTrue(store.seen("u", 20))

    def test_persistence_roundtrip_and_cap(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.json")
            s1 = ReplayStore(path=path, max_users=2, retention_windows=100)
            for u in ("a", "b", "c"):
                s1.record(u, 7, 7)
            s2 = ReplayStore(path=path, max_users=2, retention_windows=100)
            self.assertEqual(s2.user_count(), 2)  # 恢复后仍受限额约束
            self.assertTrue(s2.seen("c", 7))


# ---------------------------------------------------------------- 漂移自适应

class TestDrift(unittest.TestCase):
    def test_estimate_from_skewed_client(self):
        # 客户端时钟快 60s（恰好 2 个窗口）
        clock = FakeClock(10_000)
        totp = TOTP(KEY20, digits=6, period=30, clock=clock)
        drift = DriftEstimator(min_samples=3)
        verifier = Verifier(totp, ReplayStore(), drift=drift,
                            back=0, forward=0,          # 学习后零容忍
                            learn_back=0, learn_forward=4, user="alice")
        offsets = []
        for i in range(6):
            t_server = clock.t
            code = totp.at(t_server + 60)   # 客户端按自己的快钟生成
            self.assertTrue(verifier.verify(code, at_time=t_server),
                            "第 %d 次校验失败" % i)
            clock.advance(30)
        offsets = drift.samples("alice")
        self.assertEqual(offsets, [2] * 6)          # 漂移数据：恒为 +2 窗口
        self.assertEqual(drift.estimate("alice"), 2)
        self.assertFalse(drift.is_learning("alice"))
        # 学习期结束后候选窗口已平移到 [cur+2, cur+2]
        lo, hi = verifier._candidate_range(totp.counter_at(clock.t))
        self.assertEqual((lo, hi), (totp.counter_at(clock.t) + 2,) * 2)

    def test_without_adaptation_skewed_client_fails(self):
        # 对照组：无学习窗口时，快 60s 的客户端无法通过
        verifier, totp, clock, _ = make_verifier(
            back=0, forward=0, learn_back=0, learn_forward=0)
        clock.set(10_000)
        code = totp.at(clock.t + 60)
        self.assertFalse(verifier.verify(code, at_time=clock.t))

    def test_fractional_skew_absorbed_by_tolerance(self):
        # 快 47s：offset 在 +1/+2 间抖动，估计收敛后 ±1 容忍全覆盖
        clock = FakeClock(10_000)
        totp = TOTP(KEY20, digits=6, period=30, clock=clock)
        drift = DriftEstimator(min_samples=3)
        verifier = Verifier(totp, ReplayStore(), drift=drift,
                            back=1, forward=1,
                            learn_back=1, learn_forward=4, user="alice")
        for i in range(10):
            t_server = clock.t
            code = totp.at(t_server + 47)
            self.assertTrue(verifier.verify(code, at_time=t_server))
            clock.advance(37)  # 故意取非窗口整数倍，让 offset 抖动
        est = drift.estimate("alice")
        self.assertIn(est, (1, 2))
        for off in drift.samples("alice"):
            self.assertIn(off, (1, 2))
            self.assertLessEqual(abs(off - est), 1)  # 容忍窗口覆盖抖动


# ---------------------------------------------------------------- 时间跳变

class TestTimeJump(unittest.TestCase):
    def test_forward_jump_invalidates_old_code(self):
        verifier, totp, clock, _ = make_verifier()
        clock.set(1000)
        code = totp.now()
        self.assertTrue(verifier.verify(code))
        clock.advance(30 * 100)  # 跳到 100 个窗口之后
        old = totp.at(1000)
        self.assertFalse(verifier.verify(old))

    def test_backward_jump_within_tolerance(self):
        verifier, totp, clock, _ = make_verifier(
            back=2, forward=0, learn_back=2, learn_forward=0)
        clock.set(1000)
        code_prev = totp.at(1000 - 30)
        clock.set(1000)
        self.assertTrue(verifier.verify(code_prev))  # 回拨内容忍内仍可用

    def test_consecutive_verifications_same_window(self):
        verifier, totp, clock, _ = make_verifier()
        clock.set(500)
        self.assertTrue(verifier.verify(totp.now()))
        self.assertFalse(verifier.verify(totp.now()))  # 同窗重放拒绝
        clock.advance(30)
        self.assertTrue(verifier.verify(totp.now()))   # 新窗口恢复可用


# ---------------------------------------------------------------- 异常输入

class TestInvalidInput(unittest.TestCase):
    def test_short_key_rejected(self):
        for bad in (b"", b"short", b"123456789012345"):  # < 16 字节
            with self.assertRaises(ValueError):
                hotp(bad, 0)
            with self.assertRaises(ValueError):
                TOTP(bad)
        TOTP(b"1234567890123456")  # 恰好 16 字节合法

    def test_bad_digits_rejected(self):
        for bad in (0, 5, 11, -1):
            with self.assertRaises(ValueError):
                hotp(KEY20, 0, digits=bad)

    def test_malformed_code_rejected(self):
        verifier, totp, clock, _ = make_verifier()
        clock.set(1000)
        for bad in ("12345", "1234567", "abcdef", "12 456", "", 123456, None):
            self.assertFalse(verifier.verify(bad), repr(bad))


# ---------------------------------------------------------------- 常量时间比较

class TestConstTimeCompare(unittest.TestCase):
    def test_functional(self):
        self.assertTrue(const_time_equal("123456", "123456"))
        self.assertFalse(const_time_equal("123456", "123457"))
        self.assertFalse(const_time_equal("123456", "12345"))   # 长度不同
        self.assertFalse(const_time_equal("000000", "000001"))

    def test_no_prefix_early_exit(self):
        # 前缀匹配长度不影响返回结果的正确性（时序保证见 verifier.py 说明）
        base = "123456"
        for i in range(len(base)):
            mutated = base[:i] + ("0" if base[i] != "0" else "1") + base[i+1:]
            self.assertFalse(const_time_equal(base, mutated))


if __name__ == "__main__":
    unittest.main(verbosity=2)
