import unittest

from cookiekit import CookieJar, Policy, RejectCode


class FakeClock:
    def __init__(self, t=1700000000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds

    def jump_to(self, t):
        self.t = t


class EmptyAndMalformedTest(unittest.TestCase):
    def setUp(self):
        self.jar = CookieJar(clock=FakeClock())

    def test_empty_headers(self):
        for header in ("", "   ", None):
            r = self.jar.set_cookie(header, "https://example.com/")
            self.assertFalse(r.accepted)
            self.assertEqual(r.code, RejectCode.EMPTY_HEADER)

    def test_missing_equals_and_empty_name(self):
        r = self.jar.set_cookie("just-a-name", "https://example.com/")
        self.assertEqual(r.code, RejectCode.MISSING_EQUALS)
        r = self.jar.set_cookie("=value", "https://example.com/")
        self.assertEqual(r.code, RejectCode.EMPTY_NAME)

    def test_empty_value_accepted(self):
        r = self.jar.set_cookie("sid=", "https://example.com/")
        self.assertTrue(r.accepted)
        self.assertEqual(self.jar.cookie_header("https://example.com/"), "sid=")

    def test_oversized_value_rejected(self):
        big = "v" * 4097
        r = self.jar.set_cookie(f"big={big}", "https://example.com/")
        self.assertFalse(r.accepted)
        self.assertEqual(r.code, RejectCode.COOKIE_TOO_LONG)
        ok = "v" * (4096 - 4)
        r = self.jar.set_cookie(f"big={ok}", "https://example.com/")
        self.assertTrue(r.accepted)

    def test_invalid_attributes_ignored_lenient(self):
        r = self.jar.set_cookie(
            "a=1; Max-Age=soon; Expires=not-a-date; SameSite=loose; Unknown=x",
            "https://example.com/",
        )
        self.assertTrue(r.accepted)
        cookie = self.jar.all_cookies()[0]
        self.assertIsNone(cookie.expires_at)
        self.assertIsNone(cookie.same_site)

    def test_duplicate_name_across_paths_and_subdomains(self):
        j = self.jar
        j.set_cookie("n=root; Path=/", "https://example.com/")
        j.set_cookie("n=deep; Path=/app", "https://example.com/app")
        j.set_cookie("n=sub; Path=/", "https://sub.example.com/")
        self.assertEqual(len(j), 3)
        self.assertEqual(j.cookie_header("https://example.com/app"),
                         "n=deep; n=root")
        self.assertEqual(j.cookie_header("https://example.com/"), "n=root")
        self.assertEqual(j.cookie_header("https://sub.example.com/"), "n=sub")

    def test_host_only_and_domain_version_collapse_to_one(self):
        j = self.jar
        j.set_cookie("n=host; Path=/", "https://example.com/")
        j.set_cookie("n=wide; Domain=example.com; Path=/", "https://example.com/")
        self.assertEqual(len(j), 1)
        self.assertEqual(j.cookie_header("https://sub.example.com/"), "n=wide")


class TimeTravelTest(unittest.TestCase):
    def test_forward_jump_expires(self):
        clock = FakeClock()
        j = CookieJar(clock=clock)
        j.set_cookie("t=1; Max-Age=100; Path=/", "https://example.com/")
        clock.advance(50)
        self.assertEqual(j.cookie_header("https://example.com/"), "t=1")
        clock.advance(100)
        self.assertEqual(j.cookie_header("https://example.com/"), "")

    def test_backward_jump_keeps_cookie(self):
        clock = FakeClock()
        j = CookieJar(clock=clock)
        j.set_cookie("t=1; Max-Age=100; Path=/", "https://example.com/")
        clock.jump_to(1700000000.0 - 3600)
        self.assertEqual(j.cookie_header("https://example.com/"), "t=1")
        clock.jump_to(1700000000.0 + 99)
        self.assertEqual(j.cookie_header("https://example.com/"), "t=1")

    def test_max_age_wins_over_expires(self):
        clock = FakeClock()
        j = CookieJar(clock=clock)
        j.set_cookie("a=1; Expires=Wed, 21 Oct 2015 07:28:00 GMT; Max-Age=1000; Path=/",
                     "https://example.com/")
        self.assertEqual(j.cookie_header("https://example.com/"), "a=1")
        r = j.set_cookie("b=1; Expires=Fri, 01 Jan 2100 00:00:00 GMT; Max-Age=0; Path=/",
                         "https://example.com/")
        self.assertEqual(r.code, RejectCode.ALREADY_EXPIRED)
        self.assertEqual(j.cookie_header("https://example.com/"), "a=1")

    def test_session_cookie_survives_jumps(self):
        clock = FakeClock()
        j = CookieJar(clock=clock)
        j.set_cookie("s=1; Path=/", "https://example.com/")
        clock.advance(10 ** 9)
        self.assertEqual(j.cookie_header("https://example.com/"), "s=1")


class PortAndSchemeTest(unittest.TestCase):
    def test_port_not_scoped(self):
        j = CookieJar(clock=FakeClock())
        j.set_cookie("p=1; Path=/", "http://example.com:8080/")
        self.assertEqual(j.cookie_header("http://example.com:9090/"), "p=1")

    def test_invalid_url_rejected(self):
        j = CookieJar(clock=FakeClock())
        r = j.set_cookie("a=1", "not-a-url")
        self.assertFalse(r.accepted)
        self.assertEqual(r.code, RejectCode.INVALID_URL)


if __name__ == "__main__":
    unittest.main()
