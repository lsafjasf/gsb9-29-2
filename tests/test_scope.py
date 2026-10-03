import unittest

from cookiekit import CookieJar, RejectCode, scope
from tests.scope_cases import CASES

FIXED_NOW = 1700000000.0


def _cookie_name(header):
    return header.split(";", 1)[0].split("=", 1)[0].strip()


class ScopeCasesTest(unittest.TestCase):
    def test_scope_cases(self):
        for case in CASES:
            with self.subTest(cid=case.cid, desc=case.desc):
                jar = CookieJar(clock=lambda: FIXED_NOW)
                result = jar.set_cookie(case.header, case.set_url)
                self.assertEqual(
                    result.accepted, case.expect_accept,
                    f"{case.cid}: accepted mismatch",
                )
                self.assertEqual(
                    result.code, RejectCode[case.expect_code],
                    f"{case.cid}: reject code mismatch",
                )
                if case.expect_accept and case.request_url:
                    sent = jar.cookie_header(case.request_url)
                    actual_sent = bool(sent) and _cookie_name(case.header) in sent
                    self.assertEqual(
                        actual_sent, case.expect_sent,
                        f"{case.cid}: send mismatch, header={sent!r}",
                    )


class ScopeHelperTest(unittest.TestCase):
    def test_default_path(self):
        self.assertEqual(scope.default_path("/"), "/")
        self.assertEqual(scope.default_path("/login"), "/")
        self.assertEqual(scope.default_path("/account/login"), "/account")
        self.assertEqual(scope.default_path("/account/"), "/account")
        self.assertEqual(scope.default_path("relative"), "/")
        self.assertEqual(scope.default_path(""), "/")

    def test_path_boundary(self):
        self.assertTrue(scope.path_match("/foobar", "/"))
        self.assertFalse(scope.path_match("/foobar", "/foo"))
        self.assertTrue(scope.path_match("/foo/bar", "/foo"))
        self.assertTrue(scope.path_match("/foo", "/foo"))
        self.assertFalse(scope.path_match("/foo", "/foo/"))

    def test_public_suffix_wildcard(self):
        psl = frozenset({"com", "*.ck"})
        self.assertTrue(scope.is_public_suffix("com", psl))
        self.assertTrue(scope.is_public_suffix("foo.ck", psl))
        self.assertFalse(scope.is_public_suffix("bar.foo.ck", psl))
        self.assertFalse(scope.is_public_suffix("example.com", psl))

    def test_registrable_domain_and_same_site(self):
        psl = frozenset({"com", "co.uk"})
        self.assertEqual(scope.registrable_domain("www.example.com", psl), "example.com")
        self.assertEqual(scope.registrable_domain("shop.co.uk", psl), "shop.co.uk")
        self.assertTrue(scope.same_site("a.example.com", "b.example.com", psl))
        self.assertFalse(scope.same_site("a.example.com", "example.org", psl))
        self.assertTrue(scope.same_site("127.0.0.1", "127.0.0.1", psl))


if __name__ == "__main__":
    unittest.main()
