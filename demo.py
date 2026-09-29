#!/usr/bin/env python3
"""End-to-end demo: lazy injection, nesting, failure reporting, exposure check.

Run: python3 demo.py
"""

import io
import logging
import threading

import secret_refs as sr

CANARY = "CANARY-demo-4f8a2c"  # stands in for a real secret value


class DemoSecretService:
    """Fake secret service with a call log, to prove on-demand fetching."""

    def __init__(self, secrets):
        self._secrets = secrets
        self.call_log = []
        self.available = True

    def __call__(self, name):
        self.call_log.append(name)
        if not self.available:
            # Deliberately stuff secret material into the provider error to
            # prove the resolver strips it from its own messages.
            raise ConnectionError(f"backend down (debug: last={CANARY})")
        return self._secrets[name]


def redacted(value):
    return value[:4] + "...<redacted>" if value else value


def main():
    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    logger = logging.getLogger("demo.secret_refs")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)

    service = DemoSecretService({
        "which-db": "db/host",
        "db/host": "db.internal.example",
        "db/password": "p@ss-${secret:db/suffix}",
        "db/suffix": CANARY,
        "api/key": "api-" + CANARY,
        "loop/a": "${secret:loop/b}",
        "loop/b": "${secret:loop/a}",
    })

    failures = []
    with sr.Resolver(service, timeout=0.3, logger=logger) as resolver:
        config = sr.LazyConfig.from_json_file("config.example.json", resolver)

        print("== 1. on-demand injection ==")
        print("after loading config, call log:", service.call_log)
        host = config.get("db.host")
        print("db.host      ->", host)
        print("call log now ->", service.call_log)
        assert service.call_log == ["which-db", "db/host"], service.call_log
        assert "api/key" not in service.call_log, "unused secret was fetched!"

        print()
        print("== 2. nested references ==")
        print("db.host used a nested NAME: ${secret:${secret:which-db}}")
        password = config.get("db.password")
        print("db.password resolves via secret VALUE nesting ->", redacted(password))
        print("call log now ->", service.call_log)
        assert password == "p@ss-" + CANARY

        print()
        print("== 3. circular reference (path reported) ==")
        try:
            resolver.resolve_text("${secret:loop/a}", path="app.json:legacy")
        except sr.CircularReferenceError as exc:
            print("error:", exc)
            failures.append(exc)

        print()
        print("== 4. service unavailable / timeout ==")
        service.available = False
        service.call_log.clear()
        try:
            resolver.resolve_text("${secret:api/key}", path="app.json:api.key")
        except sr.SecretServiceError as exc:
            print("error:", exc)
            failures.append(exc)
        service.available = True

        release = threading.Event()

        def slow(name):
            release.wait(timeout=5)
            return "too-late"

        with sr.Resolver(slow, timeout=0.2, logger=logger) as slow_resolver:
            try:
                slow_resolver.resolve_text("${secret:slow/key}", path="app.json:slow")
            except sr.SecretTimeoutError as exc:
                print("error:", exc)
                failures.append(exc)
            finally:
                release.set()

        print()
        print("== 5. empty reference ==")
        try:
            resolver.resolve_text("${secret:}", path="app.json:empty")
        except sr.EmptyReferenceError as exc:
            print("error:", exc)
            failures.append(exc)

    print()
    print("== 6. exposure check ==")
    leaks = []
    if CANARY in log_stream.getvalue():
        leaks.append("log output")
    for exc in failures:
        if CANARY in str(exc) or CANARY in repr(exc):
            leaks.append(f"exception: {type(exc).__name__}")
    if leaks:
        print("FAIL: secret value leaked into", ", ".join(leaks))
        raise SystemExit(1)
    print("PASS: canary value absent from all logs and", len(failures),
          "exception messages")
    print()
    print("final secret-service call log:", service.call_log)
    print("ALL DEMO CHECKS PASSED")


if __name__ == "__main__":
    main()
