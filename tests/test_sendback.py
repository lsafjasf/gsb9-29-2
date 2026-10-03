import unittest

from cookiekit import CookieJar


class FakeClock:
    def __init__(self, t=1700000000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def build_jar():
    clock = FakeClock()
    j = CookieJar(clock=clock)
    j.set_cookie("a=1; Path=/", "https://example.com/")
    clock.advance(1)
    j.set_cookie("b=2; Path=/shop", "https://example.com/shop")
    clock.advance(1)
    j.set_cookie("c=3; Path=/shop/cart", "https://example.com/shop/cart")
    clock.advance(1)
    j.set_cookie("d=4; Path=/shop", "https://example.com/shop")
    clock.advance(1)
    j.set_cookie("sec=5; Secure; Path=/", "https://example.com/")
    clock.advance(1)
    j.set_cookie("host=6; Path=/", "https://api.example.com/")
    return j


class SendBackOrderTest(unittest.TestCase):
    def setUp(self):
        self.jar = build_jar()

    def test_deep_request_orders_by_path_length_then_creation(self):
        names = self.jar.cookies_for("https://example.com/shop/cart").names()
        self.assertEqual(names, ["c", "b", "d", "a", "sec"])

    def test_same_store_different_requests(self):
        cases = {
            "https://example.com/": ["a", "sec"],
            "https://example.com/shop": ["b", "d", "a", "sec"],
            "https://example.com/shop/cart": ["c", "b", "d", "a", "sec"],
            "https://example.com/shopper": ["a", "sec"],
            "https://example.com/shop/cart/items": ["c", "b", "d", "a", "sec"],
            "http://example.com/shop": ["b", "d", "a"],
            "https://api.example.com/": ["host"],
            "https://other.example.com/": [],
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(self.jar.cookies_for(url).names(), expected)

    def test_header_string(self):
        self.assertEqual(
            self.jar.cookie_header("https://example.com/shop/cart"),
            "c=3; b=2; d=4; a=1; sec=5",
        )

    def test_overwrite_keeps_original_order_slot(self):
        clock = FakeClock()
        j = CookieJar(clock=clock)
        j.set_cookie("x=1; Path=/p", "https://example.com/p")
        clock.advance(1)
        j.set_cookie("y=2; Path=/p", "https://example.com/p")
        clock.advance(1)
        j.set_cookie("x=9; Path=/p", "https://example.com/p")
        self.assertEqual(j.cookies_for("https://example.com/p").names(), ["x", "y"])
        self.assertEqual(j.cookie_header("https://example.com/p"), "x=9; y=2")


if __name__ == "__main__":
    unittest.main()
