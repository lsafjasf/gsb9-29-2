"""路由器：按规则顺序匹配，命中即入队到对应目的地。

匹配语义（重叠规则）：
- 规则按注册顺序逐条评估，所有命中的规则都会计入命中分布；
- 同一条记录对同一个目的地只投递一次（按目的地去重，
  保留最先命中它的规则顺序）；
- 无任何规则命中时计入 unmatched，不投递。
"""
from __future__ import annotations

from typing import Any, Mapping

from .destination import Destination, DestinationWorker


class Router:
    def __init__(self, queue_size: int = 1024):
        self._rules = []
        self._workers: "dict[str, DestinationWorker]" = {}
        self._queue_size = queue_size
        self.received = 0
        self.unmatched = 0
        self.rule_hits: "dict[str, int]" = {}

    def add_destination(self, destination: Destination,
                        queue_size: int = None) -> None:
        worker = DestinationWorker(
            destination, queue_size=queue_size or self._queue_size)
        self._workers[worker.name] = worker

    def add_rule(self, rule) -> None:
        for dest in rule.destinations:
            if dest not in self._workers:
                raise KeyError("unknown destination %r in rule %r"
                               % (dest, rule.name))
        self._rules.append(rule)
        self.rule_hits[rule.name] = 0

    def route(self, record: Mapping[str, Any]) -> list:
        """匹配并入队，返回本次命中的目的地名列表（已去重，按命中顺序）。"""
        self.received += 1
        targets = []
        seen = set()
        for rule in self._rules:
            if rule.matches(record):
                self.rule_hits[rule.name] += 1
                for dest in rule.destinations:
                    if dest not in seen:
                        seen.add(dest)
                        targets.append(dest)
        if not targets:
            self.unmatched += 1
            return []
        for dest in targets:
            self._workers[dest].enqueue(record)
        return targets

    def stats(self) -> dict:
        return {
            "received": self.received,
            "unmatched": self.unmatched,
            "rule_hits": dict(self.rule_hits),
            "destinations": {
                name: worker.stats() for name, worker in self._workers.items()
            },
        }

    def flush(self, timeout: float = 5.0) -> bool:
        return all(w.flush(timeout) for w in self._workers.values())

    def close(self, timeout: float = 5.0) -> None:
        for worker in self._workers.values():
            worker.close(timeout=timeout)
