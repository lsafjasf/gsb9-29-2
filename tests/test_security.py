import unittest

from cookiekit import CookieJar, Policy, RejectCode, SendBlock

NOW = 1700000000.0


def jar(**policy_kw):
    return CookieJar(clock=lambda: NOW, policy=Policy(**policy_kw))


class SecureSchemeTest(unittest.TestCase):
    def test_secure_set_over_http_rejected(self):
        j = jar()
        r = j.set_cookie("s=1; Secure", "http://example.com/")
        self.assertFalse(r.accepted)
        self.assertEqual(r.code, RejectCode.INSECURE_SCHEME)

    def test_secure_set_over_http_allowed_by_policy(self):
        j = jar(allow_secure_from_insecure_scheme=True)
        r = j.set_cookie("s=1; Secure", "http://example.com/")
        self.assertTrue(r.accepted)

    def test_secure_cookie_not_sent_over_http(self):
        j = jar()
        j.set_cookie("s=1; Secure; Path=/", "https://example.com/")
        result = j.cookies_for("http://example.com/")
        self.assertEqual(result.names(), [])
        self.assertEqual(result.excluded[0][1], SendBlock.NOT_SECURE_CONTEXT)
        self.assertEqual(j.cookie_header("https://example.com/"), "s=1")


class SameSiteTest(unittest.TestCase):
    def setUp(self):
        self.jar = jar()
        self.jar.set_cookie("strict=1; SameSite=Strict; Path=/", "https://example.com/")
        self.jar.set_cookie("lax=1; SameSite=Lax; Path=/", "https://example.com/")
        self.jar.set_cookie("none=1; SameSite=None; Secure; Path=/", "https://example.com/")
        self.jar.set_cookie("plain=1; Path=/", "https://example.com/")

    def test_same_site_request_sends_all(self):
        names = self.jar.cookies_for("https://example.com/", same_site="same-site").names()
        self.assertEqual(set(names), {"strict", "lax", "none", "plain"})

    def test_cross_site_strict_blocked(self):
        result = self.jar.cookies_for("https://example.com/", same_site="cross-site")
        self.assertNotIn("strict", result.names())
        reasons = {c.name: b for c, b in result.excluded}
        self.assertEqual(reasons["strict"], SendBlock.SAMESITE_STRICT)

    def test_cross_site_lax_top_level_get_allowed(self):
        result = self.jar.cookies_for("https://example.com/", same_site="cross-site",
                                      method="GET", top_level=True)
        self.assertIn("lax", result.names())

    def test_cross_site_lax_post_blocked(self):
        result = self.jar.cookies_for("https://example.com/", same_site="cross-site",
                                      method="POST", top_level=True)
        reasons = {c.name: b for c, b in result.excluded}
        self.assertEqual(reasons["lax"], SendBlock.SAMESITE_LAX)

    def test_cross_site_lax_subresource_blocked(self):
        result = self.jar.cookies_for("https://example.com/", same_site="cross-site",
                                      method="GET", top_level=False)
        reasons = {c.name: b for c, b in result.excluded}
        self.assertEqual(reasons["lax"], SendBlock.SAMESITE_LAX)

    def test_cross_site_none_sent(self):
        result = self.jar.cookies_for("https://example.com/", same_site="cross-site",
                                      method="POST", top_level=False)
        self.assertIn("none", result.names())

    def test_default_lax_for_unmarked_cookie(self):
        result = self.jar.cookies_for("https://example.com/", same_site="cross-site",
                                      method="POST", top_level=True)
        reasons = {c.name: b for c, b in result.excluded}
        self.assertEqual(reasons["plain"], SendBlock.SAMESITE_LAX)

    def test_default_lax_disabled_by_policy(self):
        j = jar(default_same_site_lax=False)
        j.set_cookie("plain=1; Path=/", "https://example.com/")
        result = j.cookies_for("https://example.com/", same_site="cross-site",
                               method="POST", top_level=True)
        self.assertIn("plain", result.names())

    def test_samesite_none_requires_secure(self):
        j = jar()
        r = j.set_cookie("n=1; SameSite=None", "https://example.com/")
        self.assertFalse(r.accepted)
        self.assertEqual(r.code, RejectCode.SAMESITE_NONE_WITHOUT_SECURE)
        j2 = jar(require_secure_for_samesite_none=False)
        self.assertTrue(j2.set_cookie("n=1; SameSite=None", "https://example.com/").accepted)


class HttpOnlyTest(unittest.TestCase):
    def test_httponly_hidden_from_non_http_api(self):
        j = jar()
        j.set_cookie("h=1; HttpOnly; Path=/", "https://example.com/")
        self.assertEqual(j.cookies_for("https://example.com/").names(), ["h"])
        result = j.cookies_for("https://example.com/", for_http=False)
        self.assertEqual(result.names(), [])
        self.assertEqual(result.excluded[0][1], SendBlock.HTTPONLY)


class StrictAttributesTest(unittest.TestCase):
    def test_invalid_samesite_rejected_in_strict_mode(self):
        j = jar(strict_attributes=True)
        r = j.set_cookie("a=1; SameSite=bogus", "https://example.com/")
        self.assertFalse(r.accepted)
        self.assertEqual(r.code, RejectCode.INVALID_ATTRIBUTE)
        j2 = jar()
        self.assertTrue(j2.set_cookie("a=1; SameSite=bogus", "https://example.com/").accepted)


if __name__ == "__main__":
    unittest.main()
