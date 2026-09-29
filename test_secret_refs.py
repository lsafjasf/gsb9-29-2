"""Self-tests for secret_refs. Run: python3 -m unittest -v"""

import json
import logging
import threading
import unittest

import secret_refs as sr

CANARY = "CANARY-9f27e1-super-secret-value"


class DictProvider:
    """In-memory secret service that records every fetch (call log)."""

    def __init__(self, secrets):
        self.secrets = dict(secrets)
        self.calls = []

    def __call__(self, name):
        self.calls.append(name)
        return self.secrets[name]  # KeyError -> SecretNotFoundError


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def make_logger():
    logger = logging.getLogger("test.secret_refs")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = ListHandler()
    logger.handlers = [handler]
    return logger, handler


class ParseTests(unittest.TestCase):
    def test_plain_text_has_no_refs(self):
        nodes = sr.parse_template("hello world")
        self.assertEqual(nodes, [sr.Text("hello world")])

    def test_simple_reference(self):
        nodes = sr.parse_template("${secret:a}")
        self.assertEqual(nodes, [sr.Ref((sr.Text("a"),))])

    def test_nested_reference_in_name(self):
        nodes = sr.parse_template("${secret:${secret:inner}}")
        self.assertEqual(nodes, [sr.Ref((sr.Ref((sr.Text("inner"),)),))])

    def test_mixed_literal_and_refs(self):
        nodes = sr.parse_template("pre-${secret:a}-mid-${secret:b}-post")
        kinds = [type(n).__name__ for n in nodes]
        self.assertEqual(kinds, ["Text", "Ref", "Text", "Ref", "Text"])

    def test_unterminated_reference_reports_path(self):
        with self.assertRaises(sr.ParseError) as ctx:
            sr.parse_template("${secret:oops", path="cfg:db.password")
        self.assertIn("cfg:db.password", str(ctx.exception))

    def test_stray_close_brace_is_literal(self):
        nodes = sr.parse_template("a}b")
        self.assertEqual(nodes, [sr.Text("a}b")])


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.logger, self.handler = make_logger()

    def resolver(self, provider, **kw):
        return sr.Resolver(provider, logger=self.logger, **kw)

    def test_plain_text_passthrough(self):
        provider = DictProvider({})
        with self.resolver(provider) as r:
            self.assertEqual(r.resolve_text("no refs here"), "no refs here")
        self.assertEqual(provider.calls, [])

    def test_simple_reference(self):
        provider = DictProvider({"a": "va"})
        with self.resolver(provider) as r:
            self.assertEqual(r.resolve_text("x=${secret:a}"), "x=va")
        self.assertEqual(provider.calls, ["a"])

    def test_nested_name_reference(self):
        provider = DictProvider({"which": "real", "real": "deep-value"})
        with self.resolver(provider) as r:
            out = r.resolve_text("${secret:${secret:which}}")
        self.assertEqual(out, "deep-value")
        self.assertEqual(provider.calls, ["which", "real"])

    def test_multilevel_value_nesting(self):
        provider = DictProvider({
            "a": "1-${secret:b}",
            "b": "2-${secret:c}",
            "c": "3-${secret:d}",
            "d": "4",
        })
        with self.resolver(provider) as r:
            out = r.resolve_text("${secret:a}", path="cfg:deep")
        self.assertEqual(out, "1-2-3-4")
        self.assertEqual(provider.calls, ["a", "b", "c", "d"])

    def test_direct_cycle_reports_path(self):
        provider = DictProvider({"a": "${secret:a}"})
        with self.resolver(provider) as r:
            with self.assertRaises(sr.CircularReferenceError) as ctx:
                r.resolve_text("${secret:a}", path="cfg:loop")
        msg = str(ctx.exception)
        self.assertIn("cfg:loop", msg)
        self.assertIn("secret:a -> secret:a", msg)

    def test_indirect_cycle_reports_full_chain(self):
        provider = DictProvider({
            "a": "${secret:b}",
            "b": "${secret:c}",
            "c": "${secret:a}",
        })
        with self.resolver(provider) as r:
            with self.assertRaises(sr.CircularReferenceError) as ctx:
                r.resolve_text("${secret:a}", path="cfg:chain")
        msg = str(ctx.exception)
        self.assertIn("secret:a -> secret:b -> secret:c -> secret:a", msg)
        self.assertTrue(msg.startswith("circular reference detected: cfg:chain"))

    def test_empty_reference_literal(self):
        provider = DictProvider({})
        with self.resolver(provider) as r:
            with self.assertRaises(sr.EmptyReferenceError) as ctx:
                r.resolve_text("${secret:}", path="cfg:empty")
        self.assertIn("cfg:empty", str(ctx.exception))
        self.assertEqual(provider.calls, [])

    def test_empty_reference_whitespace(self):
        provider = DictProvider({})
        with self.resolver(provider) as r:
            with self.assertRaises(sr.EmptyReferenceError):
                r.resolve_text("${secret:   }", path="cfg:ws")

    def test_empty_reference_via_nested_name(self):
        provider = DictProvider({"blank": ""})
        with self.resolver(provider) as r:
            with self.assertRaises(sr.EmptyReferenceError) as ctx:
                r.resolve_text("${secret:${secret:blank}}", path="cfg:nested-empty")
        self.assertIn("cfg:nested-empty", str(ctx.exception))

    def test_secret_not_found_reports_path(self):
        provider = DictProvider({})
        with self.resolver(provider) as r:
            with self.assertRaises(sr.SecretNotFoundError) as ctx:
                r.resolve_text("${secret:missing}", path="cfg:db.password")
        msg = str(ctx.exception)
        self.assertIn("missing", msg)
        self.assertIn("cfg:db.password", msg)

    def test_non_string_secret_value(self):
        provider = DictProvider({"num": 12345})
        with self.resolver(provider) as r:
            with self.assertRaises(sr.ResolutionError) as ctx:
                r.resolve_text("${secret:num}", path="cfg:num")
        self.assertNotIn("12345", str(ctx.exception))

    def test_cache_avoids_refetch(self):
        provider = DictProvider({"a": "va"})
        with self.resolver(provider) as r:
            r.resolve_text("${secret:a}")
            r.resolve_text("${secret:a}")
        self.assertEqual(provider.calls, ["a"])


class LazyInjectionTests(unittest.TestCase):
    """Prove that unused secrets are never fetched (call-log evidence)."""

    CONFIG = json.dumps({
        "db": {
            "host": "${secret:db/host}",
            "password": "${secret:db/password}",
        },
        "api": {"key": "${secret:api/key}"},
        "workers": ["${secret:w/0}", "${secret:w/1}"],
        "plain": "static",
    })

    SECRETS = {
        "db/host": "h",
        "db/password": "p",
        "api/key": "k",
        "w/0": "x",
        "w/1": "y",
    }

    def setUp(self):
        self.logger, self.handler = make_logger()
        self.provider = DictProvider(self.SECRETS)
        self.resolver = sr.Resolver(self.provider, logger=self.logger)
        self.addCleanup(self.resolver.close)
        self.config = sr.LazyConfig.from_json(
            self.CONFIG, self.resolver, source="app.json"
        )

    def test_loading_fetches_nothing(self):
        self.assertEqual(self.provider.calls, [])

    def test_get_one_leaf_fetches_only_that_secret(self):
        self.assertEqual(self.config.get("db.host"), "h")
        self.assertEqual(self.provider.calls, ["db/host"])

    def test_lazy_mapping_defers_until_key_access(self):
        db = self.config.get("db")
        self.assertIsInstance(db, sr.LazyMapping)
        self.assertEqual(self.provider.calls, [])  # nothing fetched yet
        self.assertEqual(db["password"], "p")
        self.assertEqual(self.provider.calls, ["db/password"])
        self.assertNotIn("db/host", self.provider.calls)

    def test_lazy_sequence_defers_until_index_access(self):
        workers = self.config.get("workers")
        self.assertIsInstance(workers, sr.LazySequence)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(workers[1], "y")
        self.assertEqual(self.provider.calls, ["w/1"])

    def test_plain_scalar_needs_no_fetch(self):
        self.assertEqual(self.config.get("plain"), "static")
        self.assertEqual(self.provider.calls, [])

    def test_missing_config_path(self):
        with self.assertRaises(sr.ResolutionError) as ctx:
            self.config.get("db.nope")
        self.assertIn("db.nope", str(ctx.exception))

    def test_repr_does_not_resolve(self):
        db = self.config.get("db")
        self.assertNotIn("p", repr(db).split())
        self.assertEqual(self.provider.calls, [])


class FailureModeTests(unittest.TestCase):
    def setUp(self):
        self.logger, self.handler = make_logger()

    def test_service_unavailable(self):
        def down(name):
            raise ConnectionError(f"backend down, last secret was {CANARY}")

        with sr.Resolver(down, logger=self.logger) as r:
            with self.assertRaises(sr.SecretServiceError) as ctx:
                r.resolve_text("${secret:db/password}", path="cfg:db.password")
        msg = str(ctx.exception)
        self.assertIn("ConnectionError", msg)       # error type is reported
        self.assertIn("cfg:db.password", msg)       # path is reported
        self.assertNotIn("backend down", msg)       # provider message stripped
        self.assertNotIn(CANARY, msg)               # no secret material leaked

    def test_timeout(self):
        release = threading.Event()

        def slow(name):
            release.wait(timeout=5)
            return "too-late"

        resolver = sr.Resolver(slow, timeout=0.1, logger=self.logger)
        try:
            with self.assertRaises(sr.SecretTimeoutError) as ctx:
                resolver.resolve_text("${secret:slow/key}", path="cfg:slow")
            msg = str(ctx.exception)
            self.assertIn("timeout", msg)
            self.assertIn("slow/key", msg)
            self.assertIn("cfg:slow", msg)
        finally:
            release.set()
            resolver.close()


class ExposureTests(unittest.TestCase):
    """Resolved values must never appear in logs or exception messages."""

    def test_no_canary_in_logs_or_exceptions(self):
        logger, handler = make_logger()
        secrets = {
            "canary": CANARY,
            "inner": "${secret:canary}",
            "loop": "${secret:loop}",
            "blank": "",
        }
        provider = DictProvider(secrets)
        errors = []
        with sr.Resolver(provider, logger=logger, timeout=0.2) as r:
            # 1. successful resolution: value goes to caller only
            self.assertEqual(r.resolve_text("${secret:inner}"), CANARY)
            # 2. every failure mode
            scenarios = [
                "${secret:loop}",             # cycle
                "${secret:}",                 # empty
                "${secret:missing}",          # not found
                "${secret:unterminated",      # parse error
                "${secret:${secret:blank}}",  # nested empty
            ]
            for text in scenarios:
                try:
                    r.resolve_text(text, path="cfg:probe")
                except sr.SecretRefError as exc:
                    errors.append(exc)
                else:
                    self.fail(f"expected error for {text!r}")

        def unavailable(name):
            raise RuntimeError(f"boom: {CANARY}")

        with sr.Resolver(unavailable, logger=logger) as r2:
            try:
                r2.resolve_text("${secret:x}", path="cfg:probe2")
            except sr.SecretRefError as exc:
                errors.append(exc)

        self.assertEqual(len(errors), 6)
        for exc in errors:
            self.assertNotIn(CANARY, str(exc))
            self.assertNotIn(CANARY, repr(exc))
        for message in handler.messages:
            self.assertNotIn(CANARY, message)
        # sanity: the canary really was resolved and fetched
        self.assertIn("canary", provider.calls)
        self.assertTrue(handler.messages)  # logs were actually produced


if __name__ == "__main__":
    unittest.main()
