import unittest

from cookiekit import RejectCode, parse_cookie_date, parse_set_cookie


class DateParseTest(unittest.TestCase):
    def test_rfc1123(self):
        self.assertEqual(parse_cookie_date("Wed, 21 Oct 2015 07:28:00 GMT"), 1445412480.0)

    def test_rfc850(self):
        self.assertEqual(parse_cookie_date("Sunday, 06-Nov-1994 08:49:37 GMT"), 784111777.0)

    def test_asctime(self):
        self.assertEqual(parse_cookie_date("Sun Nov  6 08:49:37 1994"), 784111777.0)

    def test_two_digit_year(self):
        self.assertEqual(parse_cookie_date("Wed, 09 Jun 21 10:18:14 GMT"),
                         1623233894.0)
        self.assertEqual(parse_cookie_date("Wed, 09 Jun 68 10:18:14 GMT"),
                         3106462694.0)
        self.assertEqual(parse_cookie_date("Wed, 09 Jun 99 10:18:14 GMT"),
                         928923494.0)

    def test_invalid(self):
        self.assertIsNone(parse_cookie_date("not a date"))
        self.assertIsNone(parse_cookie_date("Wed, 32 Oct 2015 07:28:00 GMT"))
        self.assertIsNone(parse_cookie_date("Wed, 21 Oct 2015 25:28:00 GMT"))


class SetCookieParseTest(unittest.TestCase):
    def test_basic_and_empty_value(self):
        parsed, err = parse_set_cookie("sid=abc")
        self.assertIsNone(err)
        self.assertEqual((parsed.name, parsed.value), ("sid", "abc"))
        parsed, _ = parse_set_cookie("sid=")
        self.assertEqual((parsed.name, parsed.value), ("sid", ""))
        parsed, _ = parse_set_cookie("k=a=b=c")
        self.assertEqual(parsed.value, "a=b=c")
        parsed, _ = parse_set_cookie('k="quoted"')
        self.assertEqual(parsed.value, "quoted")

    def test_all_attributes(self):
        parsed, err = parse_set_cookie(
            "sid=abc; Expires=Wed, 21 Oct 2015 07:28:00 GMT; Max-Age=60; "
            "Domain=.Example.COM; Path=/x; Secure; HttpOnly; SameSite=Lax"
        )
        self.assertIsNone(err)
        self.assertEqual(parsed.expires_at, 1445412480.0)
        self.assertEqual(parsed.max_age, 60)
        self.assertEqual(parsed.domain, "example.com")
        self.assertEqual(parsed.path, "/x")
        self.assertTrue(parsed.secure and parsed.http_only)
        from cookiekit import SameSite
        self.assertEqual(parsed.same_site, SameSite.LAX)

    def test_attribute_case_insensitive(self):
        parsed, _ = parse_set_cookie("a=1; PATH=/x; SECURE; SAMESITE=strict")
        self.assertEqual(parsed.path, "/x")
        self.assertTrue(parsed.secure)
        from cookiekit import SameSite
        self.assertEqual(parsed.same_site, SameSite.STRICT)

    def test_bad_attributes_ignored(self):
        parsed, _ = parse_set_cookie("a=1; Max-Age=soon; Expires=forever; SameSite=loose; Foo=bar")
        self.assertIsNone(parsed.max_age)
        self.assertIsNone(parsed.expires_at)
        self.assertIsNone(parsed.same_site)
        self.assertEqual(parsed.attribute_error, "SameSite='loose'")

    def test_path_must_be_absolute(self):
        parsed, _ = parse_set_cookie("a=1; Path=relative")
        self.assertIsNone(parsed.path)

    def test_empty_header_variants(self):
        self.assertEqual(parse_set_cookie("")[1], RejectCode.EMPTY_HEADER)
        self.assertEqual(parse_set_cookie("   ")[1], RejectCode.EMPTY_HEADER)
        self.assertEqual(parse_set_cookie(None)[1], RejectCode.EMPTY_HEADER)
        self.assertEqual(parse_set_cookie("no-equals")[1], RejectCode.MISSING_EQUALS)
        self.assertEqual(parse_set_cookie("=v")[1], RejectCode.EMPTY_NAME)


if __name__ == "__main__":
    unittest.main()
