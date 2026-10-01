"""差分执行与结果分类。

四类结论：
- MATCH            结果一致（同成功且值相等，或同失败且错误类别相同）
- VALUE_MISMATCH   两边都成功但解析结果不同
- OUTCOME_MISMATCH 一边成功一边失败
- ERROR_MISMATCH   两边都失败但错误类别不同
"""
from __future__ import annotations

import json
from dataclasses import dataclass

MATCH = "MATCH"
VALUE_MISMATCH = "VALUE_MISMATCH"
OUTCOME_MISMATCH = "OUTCOME_MISMATCH"
ERROR_MISMATCH = "ERROR_MISMATCH"

CATEGORY_LABELS = {
    MATCH: "结果一致",
    VALUE_MISMATCH: "两边都成功但结果不同",
    OUTCOME_MISMATCH: "一边成功一边失败",
    ERROR_MISMATCH: "两边都失败但错误类别不同",
}


@dataclass
class Outcome:
    ok: bool
    value: object = None
    error: str | None = None

    def to_json(self):
        return {"ok": self.ok, "value": self.value, "error": self.error}


def _normalize(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def run_parser(parse, data):
    """执行一个解析器，把结果/异常统一为 Outcome。"""
    try:
        return Outcome(True, _normalize(parse(data)), None)
    except Exception as exc:  # 异常类名即错误类别
        return Outcome(False, None, type(exc).__name__)


def _values_equal(x, y):
    dump = lambda v: json.dumps(v, sort_keys=True, default=repr)
    return dump(x) == dump(y)


def classify(a, b):
    if a.ok and b.ok:
        return MATCH if _values_equal(a.value, b.value) else VALUE_MISMATCH
    if not a.ok and not b.ok:
        return MATCH if a.error == b.error else ERROR_MISMATCH
    return OUTCOME_MISMATCH


def signature(category, a, b):
    """差异签名：同一签名的用例视为同一个差异，用于去重与归约判定。"""
    return (category, a.error, b.error, a.ok, b.ok)
