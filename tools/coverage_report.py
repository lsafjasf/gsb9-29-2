#!/usr/bin/env python3
"""迁移覆盖率：把回归语料跑过 FSM，报告 TRANSITIONS 表每行的命中情况。

退出码 0 = 100% 覆盖；否则列出未覆盖的迁移并退出码 1。
"""

import os
import random
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fsm_parser import TRANSITIONS, Parser
from tests.util import directed_streams, random_stream


def collect_coverage():
    coverage = Counter()
    streams = list(directed_streams())
    rng = random.Random(20260930)
    streams += [random_stream(rng) for _ in range(300)]
    for stream in streams:
        parser = Parser()
        parser.feed(stream)
        parser.eof()
        coverage.update(parser.coverage)
    return coverage, len(streams)


def main():
    coverage, n_streams = collect_coverage()
    rows = sorted(TRANSITIONS, key=lambda k: (k[0].value, k[1].value))
    print(f"语料: {n_streams} 条字节流（定向 + 随机）\n")
    print(f"{'状态':<16}{'事件':<20}{'下一状态':<16}{'命中次数':>8}")
    print("-" * 60)
    misses = []
    for key in rows:
        state, event = key
        next_state, _ = TRANSITIONS[key]
        hits = coverage.get(key, 0)
        if hits == 0:
            misses.append(key)
        print(f"{state.name:<16}{event.name:<20}{next_state.name:<16}{hits:>8}")
    total = len(rows)
    covered = total - len(misses)
    print("-" * 60)
    print(f"迁移覆盖率: {covered}/{total} = {covered / total:.1%}")
    if misses:
        print("未覆盖迁移:", ", ".join(f"{s.name}/{e.name}" for s, e in misses))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
