"""双实现比对与结论分类。"""

from dataclasses import dataclass, field

from difftest.errors import ParseError


class Verdict:
    MATCH = "MATCH"                                # 结果一致（含两边同类别失败）
    BOTH_FAIL_SAME = "BOTH_FAIL_SAME"              # 两边都失败且错误类别相同
    RESULT_MISMATCH = "RESULT_MISMATCH"            # 两边都成功但结果不同
    ERROR_CATEGORY_MISMATCH = "ERROR_CATEGORY_MISMATCH"  # 都失败但错误类别不同
    ONE_SIDED = "ONE_SIDED"                        # 一边成功一边失败

    CONSISTENT = frozenset({MATCH, BOTH_FAIL_SAME})
    DIVERGENT = frozenset({RESULT_MISMATCH, ERROR_CATEGORY_MISMATCH, ONE_SIDED})


@dataclass
class Outcome:
    status: str            # "ok" | "error"
    value: object = None   # 解析结果（status == "ok"）
    category: str = None   # 错误类别（status == "error"）
    detail: str = None

    def public(self):
        if self.status == "ok":
            return {"status": "ok", "value": self.value}
        return {"status": "error", "category": self.category,
                "detail": self.detail}


def run_one(parse_fn, data):
    """运行单个实现，归一化为 Outcome。未预期异常归为 UNEXPECTED:* 类别。"""
    try:
        return Outcome("ok", value=parse_fn(data))
    except ParseError as exc:
        return Outcome("error", category=exc.category, detail=str(exc))
    except Exception as exc:  # 实现崩溃也算一类失败，必须暴露
        return Outcome("error", category=f"UNEXPECTED:{type(exc).__name__}",
                       detail=str(exc))


def classify(out_a, out_b):
    """把两个 Outcome 归为五类结论之一。"""
    if out_a.status == "ok" and out_b.status == "ok":
        return Verdict.MATCH if out_a.value == out_b.value \
            else Verdict.RESULT_MISMATCH
    if out_a.status == "error" and out_b.status == "error":
        return Verdict.BOTH_FAIL_SAME if out_a.category == out_b.category \
            else Verdict.ERROR_CATEGORY_MISMATCH
    return Verdict.ONE_SIDED


def verdict_signature(parse_a, parse_b, data):
    """返回 (verdict, category_a, category_b)，归约时用作失败保持谓词。"""
    out_a, out_b = run_one(parse_a, data), run_one(parse_b, data)
    return (classify(out_a, out_b), out_a.category, out_b.category)


@dataclass
class Divergence:
    index: int
    data: bytes
    verdict: str
    out_a: Outcome
    out_b: Outcome
    tags: frozenset = field(default_factory=frozenset)
    minimized: bytes = None
    min_out_a: Outcome = None
    min_out_b: Outcome = None


class DifferentialRunner:
    def __init__(self, parse_a, parse_b, reducer=None, name_a="reference",
                 name_b="candidate"):
        self.parse_a = parse_a
        self.parse_b = parse_b
        self.reducer = reducer
        self.name_a = name_a
        self.name_b = name_b
        self.counts = {v: 0 for v in
                       (Verdict.MATCH, Verdict.BOTH_FAIL_SAME,
                        Verdict.RESULT_MISMATCH,
                        Verdict.ERROR_CATEGORY_MISMATCH, Verdict.ONE_SIDED)}
        self.divergences = []

    def run_case(self, index, data, tags=frozenset()):
        out_a, out_b = run_one(self.parse_a, data), run_one(self.parse_b, data)
        verdict = classify(out_a, out_b)
        self.counts[verdict] += 1
        if verdict in Verdict.DIVERGENT:
            div = Divergence(index, data, verdict, out_a, out_b, tags)
            if self.reducer is not None:
                div.minimized, div.min_out_a, div.min_out_b = \
                    self.reducer.reduce(data)
            self.divergences.append(div)
        return verdict

    def summary(self):
        total = sum(self.counts.values())
        return {
            "total": total,
            "consistent": sum(self.counts[v] for v in Verdict.CONSISTENT),
            "divergent": sum(self.counts[v] for v in Verdict.DIVERGENT),
            "verdicts": dict(self.counts),
            "implementations": {"a": self.name_a, "b": self.name_b},
        }
