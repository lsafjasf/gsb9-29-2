"""Self-tests for totp.py. Run: python3 -m unittest test_totp -v"""

import os
import tempfile
import unittest

from totp import (
    DriftEstimator,
    ReplayStore,
    Verifier,
    constant_time_equals,
    hotp,
    time_counter,
    totp,
)

# ---------------------------------------------------------------------------
# Standard test vectors
# ---------------------------------------------------------------------------

# RFC 4226 Appendix D: HOTP, secret = ASCII "12345678901234567890", 6 digits.
RFC4226_SECRET = b"12345678901234567890"
RFC4226_VECTORS = [
    (0, "755224"), (1, "287082"), (2, "359152"), (3, "969429"), (4, "338314"),
    (5, "254676"), (6, "287922"), (7, "162583"), (8, "399871"), (9, "520489"),
]

# RFC 6238 Appendix B: TOTP, 8 digits, step = 30 s.
# Keys are ASCII strings of the digit pattern, sized per digest block.
RFC6238_KEYS = {
    "sha1": b"12345678901234567890",
    "sha256": b"12345678901234567890123456789012",
    "sha512": (b"12345678901234567890123456789012"
               b"34567890123456789012345678901234"),
}
RFC6238_VECTORS = [
    # (unix_time, sha1, sha256, sha512)
    (59,          "94287082", "46119246", "90693936"),
    (1111111109,  "07081804", "68084774", "25091201"),
    (1111111111,  "14050471", "67062674", "99943326"),
    (1234567890,  "89005924", "91819424", "93441116"),
    (2000000000,  "69279037", "90698825", "38618901"),
    (20000000000, "65353130", "77737706", "47863826"),
]


class TestStandardVectors(unittest.TestCase):
    def test_rfc4226_hotp(self):
        for counter, expected in RFC4226_VECTORS:
            with self.subTest(counter=counter):
                self.assertEqual(hotp(RFC4226_SECRET, counter, 6, "sha1"),
                                 expected)

    def test_rfc6238_totp_all_digests(self):
        for t, exp1, exp256, exp512 in RFC6238_VECTORS:
            for digest, expected in (("sha1", exp1), ("sha256", exp256),
                                     ("sha512", exp512)):
                with self.subTest(t=t, digest=digest):
                    got = totp(RFC6238_KEYS[digest], for_time=t,
                               step=30, digits=8, digest=digest)
                    self.assertEqual(got, expected)

    def test_digit_widths_derived_from_rfc6238(self):
        # 6/7-digit codes must equal the low digits of the 8-digit vectors.
        for t, exp1, exp256, exp512 in RFC6238_VECTORS:
            for digest, expected8 in (("sha1", exp1), ("sha256", exp256),
                                      ("sha512", exp512)):
                for digits in (6, 7):
                    with self.subTest(t=t, digest=digest, digits=digits):
                        got = totp(RFC6238_KEYS[digest], for_time=t,
                                   step=30, digits=digits, digest=digest)
                        self.assertEqual(got, expected8[-digits:])


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation(unittest.TestCase):
    def test_short_secret_rejected(self):
        with self.assertRaises(ValueError):
            hotp(b"short", 0)
        with self.assertRaises(ValueError):
            hotp(b"0123456789abcde", 0)  # 15 bytes < 16

    def test_non_bytes_secret_rejected(self):
        with self.assertRaises(TypeError):
            hotp("12345678901234567890", 0)

    def test_bad_digits_rejected(self):
        for bad in (0, 5, 11, -1):
            with self.assertRaises(ValueError):
                hotp(RFC4226_SECRET, 0, digits=bad)

    def test_bad_digest_rejected(self):
        with self.assertRaises(ValueError):
            hotp(RFC4226_SECRET, 0, digest="md5")

    def test_bad_step_rejected(self):
        with self.assertRaises(ValueError):
            totp(RFC4226_SECRET, for_time=0, step=0)

    def test_negative_counter_rejected(self):
        with self.assertRaises(ValueError):
            hotp(RFC4226_SECRET, -1)


# ---------------------------------------------------------------------------
# Window boundaries
# ---------------------------------------------------------------------------

class TestWindowBoundaries(unittest.TestCase):
    """Window k covers the half-open interval [k*30, (k+1)*30)."""

    def test_counter_mapping(self):
        self.assertEqual(time_counter(0, 30), 0)
        self.assertEqual(time_counter(29.999, 30), 0)
        self.assertEqual(time_counter(30, 30), 1)   # exact switch instant
        self.assertEqual(time_counter(59.999, 30), 1)
        self.assertEqual(time_counter(60, 30), 2)

    def test_code_changes_exactly_at_boundary(self):
        just_before = totp(RFC4226_SECRET, for_time=29.999, step=30)
        at_boundary = totp(RFC4226_SECRET, for_time=30.0, step=30)
        self.assertNotEqual(just_before, at_boundary)
        self.assertEqual(at_boundary, hotp(RFC4226_SECRET, 1))

    def test_verify_at_switch_instant(self):
        # At t == 60 (start of window 2), with base tolerance +-1 the
        # accepted counters are {1, 2, 3}: old window 1 code still passes,
        # window 0 code does not.
        clock = lambda: 60.0
        v = Verifier(time_func=clock,
                     drift_kwargs=dict(base_spread=1, bootstrap_spread=1,
                                       bootstrap_samples=0))
        old_code = hotp(RFC4226_SECRET, 1)
        new_code = hotp(RFC4226_SECRET, 2)
        stale_code = hotp(RFC4226_SECRET, 0)
        self.assertTrue(v.verify("u_old", RFC4226_SECRET, old_code))
        self.assertTrue(v.verify("u_new", RFC4226_SECRET, new_code))
        self.assertFalse(v.verify("u_stale", RFC4226_SECRET, stale_code))


# ---------------------------------------------------------------------------
# Replay protection
# ---------------------------------------------------------------------------

class TestReplay(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.verifier = Verifier(time_func=lambda: self.now[0])

    def _code(self, t):
        return totp(RFC4226_SECRET, for_time=t, step=30)

    def test_same_code_twice_rejected(self):
        code = self._code(self.now[0])
        self.assertTrue(self.verifier.verify("u", RFC4226_SECRET, code))
        self.assertFalse(self.verifier.verify("u", RFC4226_SECRET, code))

    def test_replay_rejected_even_inside_tolerance_window(self):
        # Advance within the drift tolerance: the code still matches a
        # neighbouring window, but it was already consumed.
        code = self._code(self.now[0])
        self.assertTrue(self.verifier.verify("u", RFC4226_SECRET, code))
        self.now[0] += 30  # next window; old code is still within +-1
        res = self.verifier.verify("u", RFC4226_SECRET, code)
        self.assertFalse(res)
        self.assertEqual(res.reason, "replayed")

    def test_different_users_independent(self):
        code = self._code(self.now[0])
        self.assertTrue(self.verifier.verify("alice", RFC4226_SECRET, code))
        self.assertTrue(self.verifier.verify("bob", RFC4226_SECRET, code))

    def test_persistence_across_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "replay.json")
            store = ReplayStore(path=path)
            v1 = Verifier(store=store, time_func=lambda: self.now[0])
            code = self._code(self.now[0])
            self.assertTrue(v1.verify("u", RFC4226_SECRET, code))

            # Simulate restart: brand-new store + verifier from same file.
            store2 = ReplayStore.load(path)
            v2 = Verifier(store=store2, time_func=lambda: self.now[0])
            res = v2.verify("u", RFC4226_SECRET, code)
            self.assertFalse(res)
            self.assertEqual(res.reason, "replayed")

    def test_store_memory_bound(self):
        store = ReplayStore(max_entries=5)
        for i in range(50):
            self.assertTrue(store.check_and_record("u", i, expire_counter=i + 10))
            self.assertLessEqual(len(store), 5)

    def test_store_full_evicts_earliest_expiry(self):
        store = ReplayStore(max_entries=3)
        store.check_and_record("u", 0, expire_counter=100)
        store.check_and_record("u", 1, expire_counter=101)
        store.check_and_record("u", 2, expire_counter=102)
        store.check_and_record("u", 3, expire_counter=103)  # evicts counter 0
        self.assertEqual(len(store), 3)
        # Counter 0 was evicted (may be re-recorded); 1..3 still protected.
        self.assertFalse(store.check_and_record("u", 1, expire_counter=104))
        self.assertFalse(store.check_and_record("u", 3, expire_counter=104))

    def test_expired_entries_forgotten(self):
        store = ReplayStore(max_entries=100)
        store.check_and_record("u", 0, expire_counter=5)
        # A later record with now_counter >= 5 purges the expired one.
        store.check_and_record("u", 10, expire_counter=20)
        self.assertEqual(len(store), 1)


# ---------------------------------------------------------------------------
# Time jumps
# ---------------------------------------------------------------------------

class TestTimeJumps(unittest.TestCase):
    def test_backward_jump_does_not_crash_or_accept(self):
        now = [10_000.0]
        v = Verifier(time_func=lambda: now[0])
        code_now = totp(RFC4226_SECRET, for_time=now[0])
        self.assertTrue(v.verify("u", RFC4226_SECRET, code_now))
        now[0] = 100.0  # clock jumps backwards
        # Code from the future (pre-jump) is far outside tolerance: rejected.
        self.assertFalse(v.verify("u", RFC4226_SECRET, code_now))
        # Verification still works at the new time: no crash, normal accept.
        new_code = totp(RFC4226_SECRET, for_time=100.0)
        self.assertTrue(v.verify("u2", RFC4226_SECRET, new_code))

    def test_forward_jump_rejects_stale_code(self):
        now = [10_000.0]
        v = Verifier(time_func=lambda: now[0])
        stale = totp(RFC4226_SECRET, for_time=now[0])
        now[0] += 30 * 100  # 100 windows into the future
        self.assertFalse(v.verify("u", RFC4226_SECRET, stale))

    def test_outlier_offset_clamped_in_estimator(self):
        est = DriftEstimator(max_learn_offset=10)
        est.observe(0)
        est.observe(10_000)  # wild outlier (e.g. after a time jump)
        self.assertLessEqual(abs(est.ema), 10)


# ---------------------------------------------------------------------------
# Adaptive drift
# ---------------------------------------------------------------------------

class TestDriftAdaptation(unittest.TestCase):
    def test_window_follows_client_skew(self):
        # Client clock is 2 windows (60 s) ahead of the server.
        skew_windows = 2
        server_now = [1_000_000.0]
        v = Verifier(time_func=lambda: server_now[0],
                     drift_kwargs=dict(base_spread=1, bootstrap_spread=4,
                                       bootstrap_samples=5, alpha=0.5))
        est = v._estimator("u")
        self.assertEqual(est.window(), (-4, 4))  # permissive bootstrap

        for _ in range(8):
            client_time = server_now[0] + skew_windows * 30
            code = totp(RFC4226_SECRET, for_time=client_time)
            res = v.verify("u", RFC4226_SECRET, code)
            self.assertTrue(res)
            server_now[0] += 30

        # EMA converged to +2; after bootstrap the spread tightens to base=1.
        self.assertAlmostEqual(est.ema, 2.0, delta=0.2)
        self.assertEqual(est.window(), (1, 3))

        # A code 4 windows behind used to pass during bootstrap; now rejected.
        far_behind = totp(RFC4226_SECRET,
                          for_time=server_now[0] - 4 * 30)
        self.assertFalse(v.verify("u", RFC4226_SECRET, far_behind))

    def test_spread_widens_with_unstable_client(self):
        est = DriftEstimator(base_spread=1, bootstrap_spread=1,
                             bootstrap_samples=3, alpha=0.5)
        for off in (0, 2, 0, 2, 0, 2):
            est.observe(off)
        low, high = est.window()
        self.assertGreater(high - low, 2)  # wider than base +-1


# ---------------------------------------------------------------------------
# Constant-time comparison
# ---------------------------------------------------------------------------

class TestConstantTime(unittest.TestCase):
    def test_compare_digest_semantics(self):
        self.assertTrue(constant_time_equals("123456", "123456"))
        self.assertFalse(constant_time_equals("123456", "123457"))
        self.assertFalse(constant_time_equals("123456", "12345"))

    def test_wrong_length_input_rejected_before_compare(self):
        v = Verifier(time_func=lambda: 1000.0)
        self.assertFalse(v.verify("u", RFC4226_SECRET, "12345"))   # too short
        self.assertFalse(v.verify("u", RFC4226_SECRET, "1234567")) # too long
        self.assertFalse(v.verify("u", RFC4226_SECRET, "abcdef"))  # non-digit

    def test_timing_independent_of_prefix_match(self):
        # Statistical sanity check: mean verify time for a code sharing a
        # long prefix with the valid code must not exceed a code sharing no
        # prefix by a meaningful margin. Loose bound to stay CI-friendly.
        import time
        secret = RFC4226_SECRET
        v = Verifier(time_func=lambda: 1000.0)
        good = totp(secret, for_time=1000.0)
        prefixy = good[:5] + ("0" if good[5] != "0" else "1")
        noprefix = "".join("0" if c != "0" else "1" for c in good)

        def bench(code, n=400):
            start = time.perf_counter()
            for _ in range(n):
                v.verify("u", secret, code)
            return (time.perf_counter() - start) / n

        # Warm up, then measure; allow generous 25% relative slack.
        bench(prefixy, 50); bench(noprefix, 50)
        t_prefix = bench(prefixy)
        t_none = bench(noprefix)
        self.assertLess(t_prefix, t_none * 1.25 + 50e-6)


if __name__ == "__main__":
    unittest.main()
