"""Rule-based log routing with per-destination isolation.

Semantics
---------
Matching:
  Rules are evaluated in registration order. Every matching rule fires
  (first-match does NOT stop evaluation), so a record can hit several
  rules. Each rule maps to one or more destinations.

Dedup:
  The effective destination set of a record is the union of all matched
  rules' destinations, deduplicated with first-hit order preserved.
  A destination receives a given record at most once, no matter how many
  matched rules point to it.

Isolation:
  Each destination owns a bounded queue (``queue_size``) drained by a
  dedicated worker thread. ``route()`` only enqueues, so a slow or
  blocked destination can never stall the caller or other destinations.
  When a queue is full the record is dropped for that destination only,
  according to its drop policy, and counted.

Buffer bound:
  At most ``queue_size`` records per destination are buffered in memory;
  total in-flight records are bounded by ``sum(queue_size)`` plus one
  record being delivered per destination.
"""

from __future__ import annotations

import enum
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


class DropPolicy(enum.Enum):
    """What to do when a destination queue is full."""

    DROP_NEWEST = "drop_newest"  # reject the incoming record
    DROP_OLDEST = "drop_oldest"  # evict the oldest queued record, enqueue new


@dataclass(frozen=True)
class Rule:
    """One routing rule.

    op:
      "eq"     - record[field] == value (string compare)
      "prefix" - str(record[field]).startswith(value)
      "regex"  - re.search(value, str(record[field]))
    A record matches when the field exists and the op succeeds.
    """

    name: str
    field: str
    op: str
    value: str
    destinations: tuple

    def __post_init__(self):
        if self.op not in ("eq", "prefix", "regex"):
            raise ValueError(f"unknown op: {self.op!r}")
        if self.op == "regex":
            object.__setattr__(self, "_compiled", re.compile(self.value))

    def matches(self, record: dict) -> bool:
        if self.field not in record:
            return False
        actual = str(record[self.field])
        if self.op == "eq":
            return actual == self.value
        if self.op == "prefix":
            return actual.startswith(self.value)
        return self._compiled.search(actual) is not None


@dataclass
class LatencyStats:
    """Delivery latency (seconds) for one destination."""

    count: int = 0
    total: float = 0.0
    maximum: float = 0.0
    samples: List[float] = field(default_factory=list)

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.maximum = max(self.maximum, value)
        self.samples.append(value)

    def snapshot(self) -> dict:
        ordered = sorted(self.samples)

        def pct(p: float) -> float:
            if not ordered:
                return 0.0
            idx = min(len(ordered) - 1, int(p * len(ordered)))
            return ordered[idx]

        return {
            "count": self.count,
            "avg": self.total / self.count if self.count else 0.0,
            "p50": pct(0.50),
            "p95": pct(0.95),
            "max": self.maximum,
        }


class _Destination:
    """Bounded queue + worker thread delivering to one sink."""

    def __init__(
        self,
        name: str,
        sender: Callable[[dict], None],
        queue_size: int,
        drop_policy: DropPolicy,
    ):
        if queue_size < 1:
            raise ValueError("queue_size must be >= 1")
        self.name = name
        self.sender = sender
        self.drop_policy = drop_policy
        self.queue: "queue.Queue[dict]" = queue.Queue(maxsize=queue_size)
        self.enqueued = 0
        self.dropped = 0
        self.delivered = 0
        self.failed = 0
        self.latency = LatencyStats()
        self._lock = threading.Lock()
        self._closed = False
        self._worker = threading.Thread(
            target=self._run, name=f"logrouter-{name}", daemon=True
        )
        self._worker.start()

    def offer(self, record: dict) -> bool:
        """Enqueue a record. Returns False if it was dropped."""
        with self._lock:
            if self._closed:
                self.dropped += 1
                return False
            try:
                self.queue.put_nowait(record)
            except queue.Full:
                if self.drop_policy is DropPolicy.DROP_OLDEST:
                    try:
                        self.queue.get_nowait()
                    except queue.Empty:  # raced with worker; just drop new
                        self.dropped += 1
                        return False
                    self.dropped += 1
                    try:
                        self.queue.put_nowait(record)
                    except queue.Full:
                        self.dropped += 1
                        return False
                    self.enqueued += 1
                    return True
                self.dropped += 1
                return False
            self.enqueued += 1
            return True

    def _run(self) -> None:
        while True:
            record = self.queue.get()
            if record is _STOP:
                self.queue.task_done()
                return
            start = time.monotonic()
            try:
                self.sender(record)
            except Exception:
                with self._lock:
                    self.failed += 1
            else:
                elapsed = time.monotonic() - start
                with self._lock:
                    self.delivered += 1
                    self.latency.observe(elapsed)
            finally:
                self.queue.task_done()

    def close(self, timeout: Optional[float] = None) -> None:
        """Drain queued records (bounded by timeout) and stop the worker."""
        deadline = None if timeout is None else time.monotonic() + timeout
        stop_enqueued = False
        while True:
            with self._lock:
                self._closed = True
            if not stop_enqueued:
                try:
                    self.queue.put_nowait(_STOP)
                    stop_enqueued = True
                except queue.Full:
                    pass
            if stop_enqueued and self.queue.unfinished_tasks == 0:
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(0.001)
        self._worker.join(timeout=1.0)


_STOP = object()


@dataclass
class RouterStats:
    """Point-in-time view of routing and delivery counters."""

    routed: int
    unmatched: int
    rule_hits: Dict[str, int]
    destinations: Dict[str, dict]

    def to_dict(self) -> dict:
        return {
            "routed": self.routed,
            "unmatched": self.unmatched,
            "rule_hits": dict(self.rule_hits),
            "destinations": {k: dict(v) for k, v in self.destinations.items()},
        }


class LogRouter:
    """Routes records to destinations according to ordered rules."""

    def __init__(self, rules: Optional[List[Rule]] = None):
        self._rules: List[Rule] = list(rules or [])
        self._destinations: Dict[str, _Destination] = {}
        self._rule_hits: Dict[str, int] = {r.name: 0 for r in self._rules}
        self._routed = 0
        self._unmatched = 0
        self._lock = threading.Lock()
        self._closed = False

    def add_rule(self, rule: Rule) -> None:
        with self._lock:
            self._rules.append(rule)
            self._rule_hits.setdefault(rule.name, 0)

    def add_destination(
        self,
        name: str,
        sender: Callable[[dict], None],
        queue_size: int = 1000,
        drop_policy: DropPolicy = DropPolicy.DROP_NEWEST,
    ) -> None:
        """Register a sink. ``sender(record)`` delivers one record and may
        raise; exceptions are contained and counted as failures."""
        with self._lock:
            if name in self._destinations:
                raise ValueError(f"duplicate destination: {name!r}")
            self._destinations[name] = _Destination(
                name, sender, queue_size, drop_policy
            )

    def route(self, record: dict) -> List[str]:
        """Match ``record`` against all rules and enqueue to destinations.

        Returns the deduplicated destination names the record was sent to
        (empty when nothing matched). Never blocks on a slow destination.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("router is closed")
            targets: List[str] = []
            seen = set()
            for rule in self._rules:
                if rule.matches(record):
                    self._rule_hits[rule.name] += 1
                    for dest in rule.destinations:
                        if dest not in seen and dest in self._destinations:
                            seen.add(dest)
                            targets.append(dest)
            if targets:
                self._routed += 1
            else:
                self._unmatched += 1
            dests = [self._destinations[name] for name in targets]
        for dest in dests:
            dest.offer(record)
        return targets

    def stats(self) -> RouterStats:
        with self._lock:
            dests = {
                name: {
                    "enqueued": d.enqueued,
                    "dropped": d.dropped,
                    "delivered": d.delivered,
                    "failed": d.failed,
                    "queued": d.queue.qsize(),
                    "latency": d.latency.snapshot(),
                }
                for name, d in self._destinations.items()
            }
            return RouterStats(
                routed=self._routed,
                unmatched=self._unmatched,
                rule_hits=dict(self._rule_hits),
                destinations=dests,
            )

    def close(self, timeout: Optional[float] = 5.0) -> None:
        with self._lock:
            self._closed = True
            dests = list(self._destinations.values())
        for dest in dests:
            dest.close(timeout=timeout)

    def __enter__(self) -> "LogRouter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
