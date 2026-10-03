"""告警规则静态检查 —— 数据模型与解析。

规则输入为纯 dict（JSON 兼容），本模块负责把它解析成内部结构，
不做任何结论性判断。所有判断逻辑在 liveness.py / duplicates.py。

规则 schema::

    {
      "id": "rule-1",
      "metric": "cpu_usage",            # 单条件
      "agg": "avg",                     # 聚合方式，默认 avg
      "window": "5m",                   # 聚合窗口，默认 5m
      "for": "10m",                     # 可选：持续时长（仅参与重复判定）
      "condition": {"op": ">", "value": 90}
    }

组合条件::

    {"all": [cond, ...]}   # 与
    {"any": [cond, ...]}   # 或
    {"not": cond}          # 非

指标元数据（Metric）::

    {"name": "cpu_usage", "min": 0, "max": 100, "unit": "%",
     "integer": false, "every": "60s", "retention": "90d"}

min/max 为 None 表示该侧无界；integer=True 表示取值为整数域。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

OPS = ("<", "<=", ">", ">=", "==", "!=")

_DURATION_UNITS = {
    "ms": 0.001,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
    "w": 604800.0,
}

DEFAULT_WINDOW_SECONDS = 300.0


def parse_duration(value: Any) -> Optional[float]:
    """把 '5m' / '1h30m' / 90 / '90' 解析成秒；无法解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    total = 0.0
    pos = 0
    matched_any = False
    while pos < len(text):
        start = pos
        while pos < len(text) and (text[pos].isdigit() or text[pos] == "."):
            pos += 1
        if start == pos:
            return None
        try:
            number = float(text[start:pos])
        except ValueError:
            return None
        unit_start = pos
        while pos < len(text) and text[pos].isalpha():
            pos += 1
        unit = text[unit_start:pos]
        if unit == "" and pos == len(text) and not matched_any:
            unit = "s"  # 纯数字按秒处理
        if unit not in _DURATION_UNITS:
            return None
        total += number * _DURATION_UNITS[unit]
        matched_any = True
    return total if matched_any else None


def format_duration(seconds: Optional[float]) -> str:
    """秒数转回可读时长，如 7776000 -> '90d'、300 -> '5m'。"""
    if seconds is None:
        return "?"
    if seconds == 0:
        return "0s"
    units = (("w", 604800.0), ("d", 86400.0), ("h", 3600.0), ("m", 60.0), ("s", 1.0))
    remaining = seconds
    parts = []
    for name, size in units:
        if remaining >= size:
            value = remaining // size
            remaining -= value * size
            parts.append(f"{value:g}{name}")
    return "".join(parts) if parts else f"{seconds:g}s"


@dataclass(frozen=True)
class Metric:
    """指标元数据：取值范围 + 采样/保留信息。"""

    name: str
    min: Optional[float] = None
    max: Optional[float] = None
    integer: bool = False
    unit: str = ""
    every_seconds: Optional[float] = None
    retention_seconds: Optional[float] = None

    @staticmethod
    def from_dict(raw: Dict[str, Any]) -> "Metric":
        return Metric(
            name=str(raw["name"]),
            min=_num_or_none(raw.get("min")),
            max=_num_or_none(raw.get("max")),
            integer=bool(raw.get("integer", False)),
            unit=str(raw.get("unit", "")),
            every_seconds=parse_duration(raw.get("every")),
            retention_seconds=parse_duration(raw.get("retention")),
        )


def _num_or_none(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


@dataclass(frozen=True)
class Leaf:
    """单条比较条件：metric 经 agg/window 聚合后与 value 比较。"""

    metric: str
    op: str
    value: float
    agg: str = "avg"
    window_seconds: Optional[float] = DEFAULT_WINDOW_SECONDS
    window_raw: Any = "5m"
    for_seconds: Optional[float] = None


@dataclass(frozen=True)
class Cond:
    """条件树：kind ∈ {leaf, all, any, not}。"""

    kind: str
    leaf: Optional[Leaf] = None
    children: Tuple["Cond", ...] = ()

    @staticmethod
    def leaf_cond(leaf: Leaf) -> "Cond":
        return Cond(kind="leaf", leaf=leaf)


def parse_condition(raw: Any, inherited: Optional[Dict[str, Any]] = None) -> Cond:
    """把 dict 形式条件解析为 Cond 树。

    inherited 携带规则级默认（metric/agg/window/for），组合条件的子条件
    未显式给出这些字段时继承之。
    """
    inherited = inherited or {}
    if not isinstance(raw, dict):
        raise ValueError(f"条件必须是 dict，得到: {raw!r}")
    if "all" in raw:
        items = raw["all"]
        if not isinstance(items, list) or not items:
            raise ValueError("'all' 必须是非空列表")
        return Cond(kind="all", children=tuple(parse_condition(c, inherited) for c in items))
    if "any" in raw:
        items = raw["any"]
        if not isinstance(items, list) or not items:
            raise ValueError("'any' 必须是非空列表")
        return Cond(kind="any", children=tuple(parse_condition(c, inherited) for c in items))
    if "not" in raw:
        return Cond(kind="not", children=(parse_condition(raw["not"], inherited),))

    metric = raw.get("metric", inherited.get("metric"))
    if metric is None:
        raise ValueError(f"叶子条件缺少 metric: {raw!r}")
    op = raw.get("op", ">")
    if op not in OPS:
        raise ValueError(f"不支持的比较符: {op!r}")
    value = raw.get("value")
    num = _num_or_none(value)
    if num is None:
        raise ValueError(f"阈值必须是数字: {raw!r}")
    window_raw = raw.get("window", inherited.get("window", "5m"))
    window_seconds = parse_duration(window_raw)
    for_seconds = parse_duration(raw.get("for", inherited.get("for")))
    return Cond.leaf_cond(
        Leaf(
            metric=str(metric),
            op=op,
            value=num,
            agg=str(raw.get("agg", inherited.get("agg", "avg"))),
            window_seconds=window_seconds,
            window_raw=window_raw,
            for_seconds=for_seconds,
        )
    )


@dataclass
class Rule:
    id: str
    cond: Cond
    raw: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(raw: Dict[str, Any]) -> "Rule":
        rule_id = str(raw.get("id", raw.get("name", "<unnamed>")))
        inherited = {
            k: raw[k] for k in ("metric", "agg", "window", "for") if k in raw
        }
        if "condition" in raw:
            cond = parse_condition(raw["condition"], inherited)
        else:
            # 允许把叶子字段平铺在规则顶层
            cond = parse_condition(raw, inherited)
        return Rule(id=rule_id, cond=cond, raw=raw)


def load_metrics(raw_list: List[Dict[str, Any]]) -> Dict[str, Metric]:
    registry: Dict[str, Metric] = {}
    for raw in raw_list:
        metric = Metric.from_dict(raw)
        registry[metric.name] = metric
    return registry


def load_rules(raw_list: List[Dict[str, Any]]) -> List[Rule]:
    return [Rule.from_dict(raw) for raw in raw_list]
