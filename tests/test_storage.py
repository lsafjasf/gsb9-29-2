import unittest

from cookiekit import CookieJar, Policy, RejectCode


class FakeClock:
    def __init__(self, t=1700000000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class OverwriteTest(unittest.TestCase):
    def test_same_key_overwrites_and_keeps_creation_time(self):
        clock = FakeClock()
        j = CookieJar(clock=clock)
        j.set_cookie("sid=old; Path=/", "https://example.com/")
        clock.advance(100)
        r = j.set_cookie("sid=new; Path=/; HttpOnly", "https://example.com/")
        self.assertTrue(r.accepted and r.replaced)
        self.assertEqual(len(j), 1)
        cookie = j.all_cookies()[0]
        self.assertEqual(cookie.value, "new")
        self.assertEqual(cookie.creation_time, 1700000000.0)
        self.assertTrue(cookie.http_only)
        self.assertEqual(j.cookie_header("https://example.com/"), "sid=new")

    def test_same_name_different_path_coexist(self):
        j = CookieJar(clock=FakeClock())
        j.set_cookie("sid=root; Path=/", "https://example.com/")
        j.set_cookie("sid=deep; Path=/app", "https://example.com/app")
        self.assertEqual(len(j), 2)

    def test_host_only_and_domain_version_share_key(self):
        j = CookieJar(clock=FakeClock())
        j.set_cookie("sid=host; Path=/", "https://example.com/")
        r = j.set_cookie("sid=wide; Domain=example.com; Path=/", "https://example.com/")
        self.assertTrue(r.replaced)
        self.assertEqual(len(j), 1)
        cookie = j.all_cookies()[0]
        self.assertFalse(cookie.host_only)
        self.assertEqual(j.cookie_header("https://sub.example.com/"), "sid=wide")


class ExpiryTest(unittest.TestCase):
    def test_max_age_expiry(self):
        clock = FakeClock()
        j = CookieJar(clock=clock)
        j.set_cookie("t=1; Max-Age=10; Path=/", "https://example.com/")
        clock.advance(9)
        self.assertEqual(j.cookie_header("https://example.com/"), "t=1")
        clock.advance(2)
        self.assertEqual(j.cookie_header("https://example.com/"), "")
        self.assertEqual(len(j), 0)

    def test_expired_set_cookie_removes_existing(self):
        j = CookieJar(clock=FakeClock())
        j.set_cookie("sid=1; Path=/", "https://example.com/")
        r = j.set_cookie("sid=gone; Max-Age=0; Path=/", "https://example.com/")
        self.assertFalse(r.accepted)
        self.assertEqual(r.code, RejectCode.ALREADY_EXPIRED)
        self.assertTrue(r.removed_existing)
        self.assertEqual(len(j), 0)

    def test_expires_attribute(self):
        clock = FakeClock()
        j = CookieJar(clock=clock)
        j.set_cookie("e=1; Expires=Wed, 21 Oct 2015 07:28:00 GMT; Path=/",
                     "https://example.com/")
        self.assertEqual(len(j), 0)
        j.set_cookie("e=1; Expires=Fri, 01 Jan 2100 00:00:00 GMT; Path=/",
                     "https://example.com/")
        self.assertEqual(len(j), 1)


class EvictionTest(unittest.TestCase):
    def make_jar(self, **kw):
        clock = FakeClock()
        return CookieJar(clock=clock, policy=Policy(**kw)), clock

    def test_total_capacity_lru_order(self):
        j, clock = self.make_jar(max_total_cookies=3, max_per_domain=50)
        for name in ("a", "b", "c"):
            j.set_cookie(f"{name}=1; Path=/{name}",
                         f"https://example.com/{name}")
            clock.advance(1)
        j.cookies_for("https://example.com/a")
        j.cookies_for("https://example.com/c")
        r = j.set_cookie("d=1; Path=/d", "https://example.com/d")
        self.assertEqual([c.name for c in r.evicted], ["b"])
        self.assertEqual(sorted(c.name for c in j.all_cookies()), ["a", "c", "d"])

    def test_expired_evicted_before_lru(self):
        j, clock = self.make_jar(max_total_cookies=3, max_per_domain=50)
        j.set_cookie("old=1; Max-Age=5; Path=/", "https://example.com/")
        j.set_cookie("b=1; Path=/", "https://example.com/")
        j.set_cookie("c=1; Path=/", "https://example.com/")
        clock.advance(10)
        r = j.set_cookie("d=1; Path=/", "https://example.com/")
        self.assertEqual([c.name for c in r.evicted], ["old"])
        self.assertEqual(sorted(c.name for c in j.all_cookies()), ["b", "c", "d"])

    def test_per_domain_capacity(self):
        j, clock = self.make_jar(max_total_cookies=100, max_per_domain=2)
        j.set_cookie("a=1; Path=/", "https://example.com/")
        clock.advance(1)
        j.set_cookie("b=1; Path=/", "https://example.com/")
        clock.advance(1)
        r = j.set_cookie("c=1; Path=/", "https://example.com/")
        self.assertEqual([c.name for c in r.evicted], ["a"])
        self.assertEqual(sorted(c.name for c in j.all_cookies()), ["b", "c"])

    def test_eviction_is_deterministic(self):
        def run():
            clock = FakeClock()
            j = CookieJar(clock=clock,
                          policy=Policy(max_total_cookies=3, max_per_domain=50))
            for i in range(6):
                j.set_cookie(f"k{i}=1; Path=/", "https://example.com/")
                clock.advance(1)
            return [c.name for c in j.all_cookies()]

        self.assertEqual(run(), run())
        self.assertEqual(run(), ["k3", "k4", "k5"])


if __name__ == "__main__":
    unittest.main()
