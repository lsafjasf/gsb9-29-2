"""Software transactional memory (optimistic concurrency control).

Each committed key is a versioned cell. A transaction records a read set
{(key, version)} and a write set {key: value}. At commit time every read
version is re-checked under a global commit lock; any mismatch aborts the
whole transaction. Aborted frames (nested savepoints) merge nothing.

Only the Python standard library is used.
"""

import random
import threading
import time
from contextlib import contextmanager


class AbortTransaction(BaseException):
    """Raised by tx.abort(); inherits BaseException so user code cannot
    accidentally swallow it with `except Exception`."""


class CommitFailure(Exception):
    """Raised when a transaction cannot commit within the retry budget."""


class _Frame:
    __slots__ = ("reads", "writes", "aborted")

    def __init__(self):
        self.reads = {}   # key -> version observed
        self.writes = {}  # key -> speculative value
        self.aborted = False


class Tx:
    """Handle passed to transaction bodies (also exposed as stm.tx when a
    transaction is active on the calling thread)."""

    def __init__(self, stm):
        self._stm = stm

    def get(self, key, default=None):
        return self._stm.get(key, default)

    def set(self, key, value):
        self._stm.set(key, value)

    def abort(self):
        self._stm.abort()


class STM:
    def __init__(self, backoff=0.000001):
        self._store = {}          # key -> [version, value]
        self._commit_lock = threading.Lock()
        self._local = threading.local()
        self._backoff = backoff
        self._stat_lock = threading.Lock()
        self.reset_stats()
        self.tx = Tx(self)

    # ---- stats ---------------------------------------------------------

    def reset_stats(self):
        with self._stat_lock:
            self.attempts = 0       # transaction bodies executed
            self.conflicts = 0     # failed validation attempts
            self.retries = 0       # retried body executions
            self.commits = 0
        self._latencies = []
        self._lat_lock = threading.Lock()

    def stats(self):
        with self._stat_lock:
            with self._lat_lock:
                lat = list(self._latencies)
            return {
                "attempts": self.attempts,
                "conflicts": self.conflicts,
                "retries": self.retries,
                "commits": self.commits,
                "conflict_rate": (self.conflicts / self.attempts
                                  if self.attempts else 0.0),
                "commit_latency": self._percentiles(lat),
            }

    @staticmethod
    def _percentiles(values):
        if not values:
            return {"count": 0, "p50": 0.0, "p95": 0.0, "max": 0.0,
                    "mean": 0.0}
        ordered = sorted(values)

        def pct(p):
            idx = min(len(ordered) - 1,
                      int(round((p / 100.0) * (len(ordered) - 1))))
            return ordered[idx]

        return {
            "count": len(ordered),
            "p50": pct(50),
            "p95": pct(95),
            "max": ordered[-1],
            "mean": sum(ordered) / len(ordered),
        }

    # ---- frame stack ---------------------------------------------------

    def _frames(self):
        stack = getattr(self._local, "frames", None)
        if stack is None:
            stack = []
            self._local.frames = stack
        return stack

    def _require_frame(self):
        frames = self._frames()
        if not frames:
            raise RuntimeError("no active transaction on this thread")
        return frames

    # ---- transactional operations -------------------------------------

    def get(self, key, default=None):
        frames = self._require_frame()
        for frame in reversed(frames):
            if key in frame.writes:
                return frame.writes[key]
        with self._commit_lock:
            cell = self._store.get(key)
            if cell is None:
                value, version = default, 0
            else:
                version, value = cell[0], cell[1]
        frames[-1].reads.setdefault(key, version)
        return value

    def set(self, key, value):
        self._require_frame()[-1].writes[key] = value

    def abort(self):
        self._require_frame()[-1].aborted = True
        raise AbortTransaction()

    def snapshot(self):
        with self._commit_lock:
            return {key: cell[1] for key, cell in self._store.items()}

    def get_committed(self, key, default=None):
        with self._commit_lock:
            cell = self._store.get(key)
            return default if cell is None else cell[1]

    # ---- nested transactions / savepoints ------------------------------

    @contextmanager
    def savepoint(self):
        frames = self._frames()
        if not frames:
            raise RuntimeError("savepoint() must be used inside a transaction")
        frame = _Frame()
        frames.append(frame)
        try:
            yield self.tx
        except AbortTransaction:
            frame.aborted = True
        except BaseException:
            frame.aborted = True
            raise
        finally:
            frames.pop()
            if not frame.aborted:
                parent = frames[-1]
                for key, version in frame.reads.items():
                    parent.reads.setdefault(key, version)
                parent.writes.update(frame.writes)

    # ---- top-level execution -------------------------------------------

    def run(self, fn, retries=10, *args, **kwargs):
        if self._frames():
            raise RuntimeError("nested transactions must use savepoint()")
        frames = self._frames()
        frame = _Frame()
        frames.append(frame)
        try:
            for attempt in range(retries + 1):
                with self._stat_lock:
                    self.attempts += 1
                try:
                    result = fn(self.tx, *args, **kwargs)
                except AbortTransaction:
                    return None
                if self._try_commit(frame):
                    return result
                with self._stat_lock:
                    self.conflicts += 1
                if attempt == retries:
                    raise CommitFailure(
                        "commit failed after %d attempt(s)" % (retries + 1))
                with self._stat_lock:
                    self.retries += 1
                self._backoff_sleep(attempt)
                frame = _Frame()
                frames[-1] = frame
        finally:
            frames.pop()

    def _backoff_sleep(self, attempt):
        time.sleep(min(self._backoff * (2 ** attempt), 0.001)
                   * random.random())

    def _try_commit(self, frame):
        start = time.perf_counter()
        with self._commit_lock:
            for key, version in frame.reads.items():
                cell = self._store.get(key)
                current = 0 if cell is None else cell[0]
                if current != version:
                    return False
            for key, value in frame.writes.items():
                cell = self._store.get(key)
                if cell is None:
                    self._store[key] = [1, value]
                else:
                    cell[0] += 1
                    cell[1] = value
        elapsed = time.perf_counter() - start
        with self._stat_lock:
            self.commits += 1
        with self._lat_lock:
            self._latencies.append(elapsed)
        return True

    def atomic(self, retries=10):
        def decorate(fn):
            def wrapper(*args, **kwargs):
                return self.run(fn, retries, *args, **kwargs)
            return wrapper
        return decorate
