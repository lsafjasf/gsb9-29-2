"""Run the SUT in a forked worker so crashes and hangs never kill the fuzzer.

Protocol: the parent sends ``(data, timeout)`` over a pipe; the worker replies
``(status, bug_class, detail)``.  A hang is detected by the parent waiting on
``poll(timeout)``; the stuck worker is killed and replaced.
"""

from __future__ import annotations

import multiprocessing as mp
import sys
from typing import Iterator, Optional, Tuple

from .sut import ProtocolError, parse_message

STATUS_OK = "ok"
STATUS_REJECT = "reject"
STATUS_CRASH = "crash"
STATUS_TIMEOUT = "timeout"

CRASH_CLASSES = {
    "AssertionError": "length_desync",
    "RecursionError": "depth_overflow",
    "MemoryError": "oversize_alloc",
}
TIMEOUT_CLASS = "hang_sentinel"


def _worker(conn) -> None:
    sys.setrecursionlimit(10000)
    while True:
        try:
            task = conn.recv()
        except (EOFError, KeyboardInterrupt):
            return
        if task is None:
            return
        data, _timeout = task
        try:
            parse_message(data)
            reply = (STATUS_OK, "", "")
        except ProtocolError as exc:
            reply = (STATUS_REJECT, "", str(exc))
        except Exception as exc:  # crash bucket
            cls = type(exc).__name__
            reply = (STATUS_CRASH, CRASH_CLASSES.get(cls, cls), str(exc)[:200])
        try:
            conn.send(reply)
        except (BrokenPipeError, OSError):
            return


class SafeRunner:
    def __init__(self, timeout: float = 0.25):
        self.timeout = timeout
        self._proc: Optional[mp.Process] = None
        self._parent = None
        self.restarts = 0
        self._spawn()

    def _spawn(self) -> None:
        self._parent, child = mp.Pipe()
        self._proc = mp.Process(target=_worker, args=(child,), daemon=True)
        self._proc.start()

    def _kill(self) -> None:
        if self._proc is not None and self._proc.is_alive():
            self._proc.kill()
        if self._proc is not None:
            self._proc.join(timeout=2.0)
        try:
            self._parent.close()
        except Exception:
            pass

    def _restart(self) -> None:
        self._kill()
        self.restarts += 1
        self._spawn()

    def run(self, data: bytes, timeout: Optional[float] = None
            ) -> Tuple[str, str, str]:
        limit = self.timeout if timeout is None else timeout
        try:
            self._parent.send((bytes(data), limit))
        except (BrokenPipeError, OSError):
            self._restart()
            self._parent.send((bytes(data), limit))
        if self._parent.poll(limit):
            try:
                return self._parent.recv()
            except (EOFError, OSError):
                self._restart()
                return (STATUS_CRASH, "worker_died", "worker exited without reply")
        self._restart()
        return (STATUS_TIMEOUT, TIMEOUT_CLASS, "exceeded %.3fs" % limit)

    def run_many(self, items, timeout: Optional[float] = None
                 ) -> Iterator[Tuple[bytes, str, str, str]]:
        for data in items:
            status, cls, detail = self.run(data, timeout)
            yield data, status, cls, detail

    def close(self) -> None:
        try:
            self._parent.send(None)
        except Exception:
            pass
        self._kill()

    def __enter__(self) -> "SafeRunner":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
