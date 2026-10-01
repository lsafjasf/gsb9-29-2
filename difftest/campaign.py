"""差分测试主流程：生成 → 对比 → 归约 → 报告。"""
from __future__ import annotations

from dataclasses import dataclass

from . import harness
from .coverage import Coverage
from .generator import Generator
from .reducer import shrink
from .report import write_report


@dataclass
class DiffConfig:
    cases: int = 500
    seed: int = 1
    gen_max_depth: int = 6
    nesting_depth: int = 12
    deep_depth: int = 100
    oversize_bytes: int = 5000
    shrink_budget: int = 4000
    outdir: str = "difftest_out"


@dataclass
class Mismatch:
    category: str
    tag: str
    original: bytes
    minimized: bytes
    out_a: harness.Outcome
    out_b: harness.Outcome


class Campaign:
    """一次差分测试活动。按签名去重，每个签名保留最小的反例。"""

    def __init__(self, spec, parse_a, parse_b, cfg):
        self.spec = spec
        self.parse_a = parse_a
        self.parse_b = parse_b
        self.cfg = cfg

    def _signature_of(self, data):
        a = harness.run_parser(self.parse_a, data)
        b = harness.run_parser(self.parse_b, data)
        return harness.signature(harness.classify(a, b), a, b)

    def run(self):
        gen = Generator(self.spec, self.cfg, self.cfg.seed)
        coverage = Coverage()
        mismatches = {}

        for case in gen.cases(self.cfg.cases):
            if case.data is None:
                coverage.record_skip(case.meta.get("edge"))
                continue
            a = harness.run_parser(self.parse_a, case.data)
            b = harness.run_parser(self.parse_b, case.data)
            category = harness.classify(a, b)
            coverage.record(case, a, b, category)
            if category == harness.MATCH:
                continue

            sig = harness.signature(category, a, b)
            minimized = shrink(
                case.data,
                lambda data, s=sig: self._signature_of(data) == s,
                budget=self.cfg.shrink_budget,
            )
            out_a = harness.run_parser(self.parse_a, minimized)
            out_b = harness.run_parser(self.parse_b, minimized)
            current = mismatches.get(sig)
            if current is None or len(minimized) < len(current.minimized):
                mismatches[sig] = Mismatch(
                    category, case.tag, case.data, minimized, out_a, out_b
                )

        return write_report(self.cfg.outdir, list(mismatches.values()), coverage, self.cfg)
