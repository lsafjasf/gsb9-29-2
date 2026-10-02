"""secretref 自测：解析结果、暴露面检查、边界用例。

运行：python3 -m unittest test_secretref -v
"""
import io
import logging
import unittest

from secretref import (
    CycleError,
    DepthExceededError,
    EmptyRefError,
    LazyConfig,
    RefParseError,
    Resolver,
    SecretRefError,
    SecretTimeoutError,
    SecretUnavailableError,
    UnterminatedRefError,
)

SECRET_VALUE = "s3cr3t-Do-Not-Leak"


class FakeService:
    """记录调用日志的假密钥服务，用于证明按需注入与调用顺序。"""

    def __init__(self, mapping=None, fail=None):
        self.mapping = dict(mapping or {})
        self.fail = dict(fail or {})  # key -> 异常实例或可调用
        self.calls = []  # [(key, timeout), ...]

    def get(self, key, *, timeout=None):
        self.calls.append((key, timeout))
        if key in self.fail:
            err = self.fail[key]
            raise err() if callable(err) else err
        if key not in self.mapping:
            raise KeyError(f"no such secret: {key}")
        return self.mapping[key]


class TestResolve(unittest.TestCase):
    def test_plain_string_no_service_call(self):
        svc = FakeService()
        r = Resolver(svc)
        self.assertEqual(r.resolve("plain text"), "plain text")
        self.assertEqual(svc.calls, [])

    def test_simple_reference(self):
        svc = FakeService({"db/pass": SECRET_VALUE})
        r = Resolver(svc)
        self.assertEqual(r.resolve("${secret:db/pass}"), SECRET_VALUE)
        self.assertEqual([c[0] for c in svc.calls], ["db/pass"])

    def test_reference_embedded_in_text(self):
        svc = FakeService({"db/host": "10.0.0.1", "db/port": "5432"})
        r = Resolver(svc)
        out = r.resolve("pg://${secret:db/host}:${secret:db/port}/app")
        self.assertEqual(out, "pg://10.0.0.1:5432/app")

    def test_multi_level_nesting(self):
        svc = FakeService({
            "a": "x-${secret:b}",
            "b": "y-${secret:c}",
            "c": SECRET_VALUE,
        })
        r = Resolver(svc)
        self.assertEqual(r.resolve("${secret:a}"), f"x-y-{SECRET_VALUE}")
        self.assertEqual([c[0] for c in svc.calls], ["a", "b", "c"])

    def test_secret_value_containing_dollar_brace_literal(self):
        svc = FakeService({"k": "cost is $5 {ok}"})
        r = Resolver(svc)
        self.assertEqual(r.resolve("${secret:k}"), "cost is $5 {ok}")


class TestLazyInjection(unittest.TestCase):
    def test_unused_keys_never_fetched(self):
        svc = FakeService({
            "used": SECRET_VALUE,
            "unused1": "v1",
            "unused2": "v2",
        })
        cfg = LazyConfig(
            {
                "db_password": "${secret:used}",
                "api_token": "${secret:unused1}",
                "ssh_key": "${secret:unused2}",
                "plain": "not-a-secret",
            },
            Resolver(svc),
        )
        # 构造 LazyConfig 本身不得触发任何请求
        self.assertEqual(svc.calls, [])
        # 访问非引用配置也不触发
        self.assertEqual(cfg["plain"], "not-a-secret")
        self.assertEqual(svc.calls, [])
        # 只访问用到的键
        self.assertEqual(cfg["db_password"], SECRET_VALUE)
        self.assertEqual([c[0] for c in svc.calls], ["used"])
        # 缓存：再次访问不重复取
        self.assertEqual(cfg["db_password"], SECRET_VALUE)
        self.assertEqual([c[0] for c in svc.calls], ["used"])
        # 未使用的键始终未被取出
        fetched = [c[0] for c in svc.calls]
        self.assertNotIn("unused1", fetched)
        self.assertNotIn("unused2", fetched)
        print("\n[调用日志] secret service calls:", svc.calls)

    def test_missing_config_key(self):
        cfg = LazyConfig({}, Resolver(FakeService()))
        self.assertEqual(cfg.get("nope", "fallback"), "fallback")
        with self.assertRaises(KeyError):
            cfg["nope"]


class TestCycles(unittest.TestCase):
    def test_self_cycle(self):
        svc = FakeService({"a": "${secret:a}"})
        r = Resolver(svc)
        with self.assertRaises(CycleError) as cm:
            r.resolve("${secret:a}", origin="config:k")
        msg = str(cm.exception)
        self.assertIn("config:k -> secret:a -> secret:a", msg)

    def test_indirect_cycle_reports_path(self):
        svc = FakeService({
            "a": "${secret:b}",
            "b": "${secret:c}",
            "c": "${secret:a}",
        })
        r = Resolver(svc)
        with self.assertRaises(CycleError) as cm:
            r.resolve("${secret:a}", origin="config:start")
        msg = str(cm.exception)
        self.assertIn(
            "config:start -> secret:a -> secret:b -> secret:c -> secret:a", msg
        )
        self.assertNotIn(SECRET_VALUE, msg)

    def test_depth_limit(self):
        svc = FakeService()
        state = {"n": 0}

        def endless_get(key, *, timeout=None):
            # 每次引用一个从未出现过的新 key，绕开环检测，靠深度上限兜底
            svc.calls.append((key, timeout))
            state["n"] += 1
            return "${secret:k%d}" % state["n"]

        svc.get = endless_get
        r = Resolver(svc, max_depth=8)
        with self.assertRaises(DepthExceededError):
            r.resolve("${secret:k0}")


class TestEdgeCases(unittest.TestCase):
    def test_empty_reference(self):
        r = Resolver(FakeService())
        with self.assertRaises(EmptyRefError) as cm:
            r.resolve("pw=${secret:}", origin="config:db.password")
        self.assertIn("config:db.password", str(cm.exception))

    def test_whitespace_only_reference(self):
        r = Resolver(FakeService())
        with self.assertRaises(EmptyRefError):
            r.resolve("${secret:   }")

    def test_unterminated_reference(self):
        r = Resolver(FakeService())
        with self.assertRaises(UnterminatedRefError):
            r.resolve("pw=${secret:db/pass")
        with self.assertRaises(RefParseError):
            r.resolve("pw=${:oops")

    def test_service_unavailable(self):
        svc = FakeService(fail={"db/pass": ConnectionError("refused")})
        r = Resolver(svc)
        with self.assertRaises(SecretUnavailableError) as cm:
            r.resolve("${secret:db/pass}", origin="config:db.password")
        msg = str(cm.exception)
        self.assertIn("db/pass", msg)
        self.assertIn("config:db.password", msg)

    def test_service_timeout(self):
        svc = FakeService(fail={"slow/key": TimeoutError("deadline exceeded")})
        r = Resolver(svc, timeout=0.5)
        with self.assertRaises(SecretTimeoutError) as cm:
            r.resolve("${secret:slow/key}")
        self.assertIn("slow/key", str(cm.exception))
        # 超时参数确实传给了密钥服务
        self.assertEqual(svc.calls, [("slow/key", 0.5)])

    def test_unknown_key(self):
        svc = FakeService()
        r = Resolver(svc)
        with self.assertRaises(SecretUnavailableError):
            r.resolve("${secret:no/such}")

    def test_non_string_secret(self):
        svc = FakeService({"k": 12345})
        r = Resolver(svc)
        with self.assertRaises(SecretUnavailableError):
            r.resolve("${secret:k}")


class TestExposureSurface(unittest.TestCase):
    """暴露面检查：解析结果不得出现在日志与异常信息里。"""

    def assert_no_leak(self, text):
        self.assertNotIn(SECRET_VALUE, text)
        self.assertNotIn("s3cr3t", text)

    def test_error_messages_do_not_contain_resolved_values(self):
        svc = FakeService({
            "good": SECRET_VALUE,
            "loop": "${secret:loop}",
        })
        r = Resolver(svc)
        # 先解析出一个真实密钥值
        self.assertEqual(r.resolve("${secret:good}"), SECRET_VALUE)
        # 再制造各种错误，检查消息不含已解析出的值
        errors = []
        for text in ("${secret:loop}", "${secret:}", "${secret:missing}", "x=${secret:bad"):
            try:
                r.resolve(text, origin="config:probe")
            except SecretRefError as exc:
                errors.append(exc)
                self.assert_no_leak(str(exc))
                self.assert_no_leak(repr(exc))
        self.assertEqual(len(errors), 4)

    def test_service_exception_chain_is_suppressed(self):
        # 密钥服务自己的异常消息里夹带了密钥值，包装后必须不外泄
        svc = FakeService(fail={"k": RuntimeError(f"backend said: {SECRET_VALUE}")})
        r = Resolver(svc)
        with self.assertRaises(SecretUnavailableError) as cm:
            r.resolve("${secret:k}")
        exc = cm.exception
        self.assert_no_leak(str(exc))
        self.assertTrue(exc.__suppress_context__)
        import traceback
        self.assert_no_leak("".join(traceback.format_exception(exc)))

    def test_no_secret_in_logs(self):
        svc = FakeService({
            "a": "${secret:b}",
            "b": SECRET_VALUE,
            "loop": "${secret:loop}",
        })
        r = Resolver(svc)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            r.resolve("${secret:a}")  # 成功路径
            try:
                r.resolve("${secret:loop}")  # 失败路径
            except SecretRefError:
                pass
        finally:
            root.removeHandler(handler)
        self.assert_no_leak(stream.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
