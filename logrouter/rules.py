"""路由规则：字段相等 / 前缀 / 正则 三种匹配器。"""
from __future__ import annotations

import re
from typing import Any, Mapping


class Rule:
    """一条路由规则：匹配器 + 目的地名。

    name 用于命中分布统计；destinations 为该规则命中后要投递的目的地。
    """

    def __init__(self, name: str, destinations):
        if not destinations:
            raise ValueError("rule %r must have at least one destination" % name)
        self.name = name
        self.destinations = list(destinations)

    def matches(self, record: Mapping[str, Any]) -> bool:  # pragma: no cover
        raise NotImplementedError


class FieldEquals(Rule):
    """record[field] == value（缺失字段视为不匹配）。"""

    def __init__(self, name, field, value, destinations):
        super().__init__(name, destinations)
        self.field = field
        self.value = value

    def matches(self, record):
        return record.get(self.field, _MISSING) == self.value


class FieldPrefix(Rule):
    """str(record[field]) 以 prefix 开头。"""

    def __init__(self, name, field, prefix, destinations):
        super().__init__(name, destinations)
        self.field = field
        self.prefix = prefix

    def matches(self, record):
        value = record.get(self.field, _MISSING)
        return value is not _MISSING and str(value).startswith(self.prefix)


class FieldRegex(Rule):
    """str(record[field]) 被正则匹配（re.search 语义）。"""

    def __init__(self, name, field, pattern, destinations):
        super().__init__(name, destinations)
        self.field = field
        self.pattern = re.compile(pattern)

    def matches(self, record):
        value = record.get(self.field, _MISSING)
        return value is not _MISSING and self.pattern.search(str(value)) is not None


class _Missing:
    def __repr__(self):  # pragma: no cover
        return "<missing>"


_MISSING = _Missing()
