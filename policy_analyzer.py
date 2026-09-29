"""访问策略规则分析库（仅标准库）。

把每条规则建模为四维超矩形：源网段 x 目的网段 x 协议集合 x 端口区间。
用矩形差集算法精确计算规则间的覆盖关系，并检出三类问题：

1. redundant     冗余规则：其全部匹配空间已被更早的同动作规则覆盖，删除不影响任何报文的判定。
2. contradiction 矛盾规则对：动作相反且匹配空间相交，相交区间的判定结果取决于规则顺序。
3. never_hit     永不命中：其全部匹配空间被更早的规则（含相反动作）覆盖，永远不会被触发。

每条判定都附带涉及规则编号与覆盖区间内的示例报文作为依据。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from dataclasses import dataclass, field

ALL_PROTOCOLS = frozenset({"tcp", "udp", "icmp"})
PORT_MIN, PORT_MAX = 0, 65535


# ---------------------------------------------------------------- 规则模型

@dataclass(frozen=True)
class Rect:
    """规则匹配空间：四维超矩形。"""
    src_lo: int
    src_hi: int
    dst_lo: int
    dst_hi: int
    protos: frozenset
    port_lo: int
    port_hi: int


@dataclass
class Rule:
    rid: object
    action: str          # "allow" | "deny"
    rect: Rect
    raw: dict = field(default_factory=dict)


def _parse_network(text):
    if text in ("*", "any", None):
        net = ipaddress.ip_network("0.0.0.0/0")
    else:
        net = ipaddress.ip_network(str(text), strict=False)
    if net.version != 4:
        raise ValueError(f"仅支持 IPv4 网段: {text}")
    return int(net.network_address), int(net.broadcast_address)


def _parse_ports(text):
    if text in ("*", "any", None):
        return PORT_MIN, PORT_MAX
    s = str(text).strip()
    if "-" in s:
        lo, hi = s.split("-", 1)
        lo, hi = int(lo), int(hi)
    else:
        lo = hi = int(s)
    if not (PORT_MIN <= lo <= hi <= PORT_MAX):
        raise ValueError(f"非法端口区间: {text}")
    return lo, hi


def _parse_protocols(text):
    if text in ("*", "any", None):
        return ALL_PROTOCOLS
    protos = frozenset(p.strip().lower() for p in str(text).split(","))
    unknown = protos - ALL_PROTOCOLS
    if unknown:
        raise ValueError(f"未知协议: {sorted(unknown)}")
    return protos


def parse_rule(obj, idx=0):
    """把 dict 解析为 Rule。字段: id/action/src/dst/protocol/port。"""
    action = str(obj.get("action", "")).lower()
    if action not in ("allow", "deny"):
        raise ValueError(f"规则 {obj.get('id', idx)}: action 必须是 allow 或 deny")
    src_lo, src_hi = _parse_network(obj.get("src", "*"))
    dst_lo, dst_hi = _parse_network(obj.get("dst", "*"))
    port_lo, port_hi = _parse_ports(obj.get("port", "*"))
    protos = _parse_protocols(obj.get("protocol", "*"))
    rect = Rect(src_lo, src_hi, dst_lo, dst_hi, protos, port_lo, port_hi)
    return Rule(rid=obj.get("id", idx), action=action, rect=rect, raw=obj)


def load_rules(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError("规则文件必须是 JSON 数组")
    return [parse_rule(obj, i + 1) for i, obj in enumerate(data)]


# ---------------------------------------------------------------- 矩形运算

def _sub_interval(lo, hi, olo, ohi):
    """[lo,hi] 减去 [olo,ohi]，返回剩余区间列表。"""
    out = []
    if lo < olo:
        out.append((lo, olo - 1))
    if hi > ohi:
        out.append((ohi + 1, hi))
    return [(a, b) for a, b in out if a <= b]


def subtract(rect, other):
    """rect 减去 other，返回不相交的剩余矩形列表（可能为空）。

    沿每个维度依次：把 cur 在该维度上落在 other 之外的部分直接收入结果，
    再把 cur 收窄到与 other 在该维度的交集，继续处理下一维度。
    """
    if intersection(rect, other) is None:
        return [rect]
    result = []
    cur = rect
    for a, b in _sub_interval(cur.src_lo, cur.src_hi, other.src_lo, other.src_hi):
        result.append(Rect(a, b, cur.dst_lo, cur.dst_hi, cur.protos,
                           cur.port_lo, cur.port_hi))
    cur = Rect(max(cur.src_lo, other.src_lo), min(cur.src_hi, other.src_hi),
               cur.dst_lo, cur.dst_hi, cur.protos, cur.port_lo, cur.port_hi)
    for a, b in _sub_interval(cur.dst_lo, cur.dst_hi, other.dst_lo, other.dst_hi):
        result.append(Rect(cur.src_lo, cur.src_hi, a, b, cur.protos,
                           cur.port_lo, cur.port_hi))
    cur = Rect(cur.src_lo, cur.src_hi,
               max(cur.dst_lo, other.dst_lo), min(cur.dst_hi, other.dst_hi),
               cur.protos, cur.port_lo, cur.port_hi)
    rest_protos = cur.protos - other.protos
    if rest_protos:
        result.append(Rect(cur.src_lo, cur.src_hi, cur.dst_lo, cur.dst_hi,
                           rest_protos, cur.port_lo, cur.port_hi))
    cur = Rect(cur.src_lo, cur.src_hi, cur.dst_lo, cur.dst_hi,
               cur.protos & other.protos, cur.port_lo, cur.port_hi)
    for a, b in _sub_interval(cur.port_lo, cur.port_hi, other.port_lo, other.port_hi):
        result.append(Rect(cur.src_lo, cur.src_hi, cur.dst_lo, cur.dst_hi,
                           cur.protos, a, b))
    return result


def remaining(rect, others):
    """rect 减去 others 的并集后剩余的不相交矩形列表。为空表示被完全覆盖。"""
    pieces = [rect]
    for other in others:
        nxt = []
        for p in pieces:
            nxt.extend(subtract(p, other))
        pieces = nxt
        if not pieces:
            break
    return pieces


def intersection(a, b):
    """两个矩形的交集，不相交返回 None。"""
    src_lo, src_hi = max(a.src_lo, b.src_lo), min(a.src_hi, b.src_hi)
    dst_lo, dst_hi = max(a.dst_lo, b.dst_lo), min(a.dst_hi, b.dst_hi)
    port_lo, port_hi = max(a.port_lo, b.port_lo), min(a.port_hi, b.port_hi)
    protos = a.protos & b.protos
    if src_lo > src_hi or dst_lo > dst_hi or port_lo > port_hi or not protos:
        return None
    return Rect(src_lo, src_hi, dst_lo, dst_hi, protos, port_lo, port_hi)


def covers(a, b):
    """a 是否完整覆盖 b（b 的匹配空间是 a 的子集）。"""
    return (a.src_lo <= b.src_lo and a.src_hi >= b.src_hi
            and a.dst_lo <= b.dst_lo and a.dst_hi >= b.dst_hi
            and a.port_lo <= b.port_lo and a.port_hi >= b.port_hi
            and b.protos <= a.protos)


def example_packet(rect):
    """取矩形内一个确定性的示例报文，作为判定依据。"""
    return {
        "src": str(ipaddress.ip_address(rect.src_lo)),
        "dst": str(ipaddress.ip_address(rect.dst_lo)),
        "protocol": sorted(rect.protos)[0],
        "port": rect.port_lo,
    }


def describe_rect(rect):
    def ipr(lo, hi):
        if lo == 0 and hi == 0xFFFFFFFF:
            return "*"
        if lo == hi:
            return str(ipaddress.ip_address(lo))
        return f"{ipaddress.ip_address(lo)}-{ipaddress.ip_address(hi)}"

    def pr(lo, hi):
        if lo == PORT_MIN and hi == PORT_MAX:
            return "*"
        return str(lo) if lo == hi else f"{lo}-{hi}"

    protos = "*" if rect.protos == ALL_PROTOCOLS else ",".join(sorted(rect.protos))
    return (f"src={ipr(rect.src_lo, rect.src_hi)} "
            f"dst={ipr(rect.dst_lo, rect.dst_hi)} "
            f"proto={protos} port={pr(rect.port_lo, rect.port_hi)}")


# ---------------------------------------------------------------- 分析

@dataclass
class Finding:
    kind: str            # redundant | contradiction | never_hit
    rules: list          # 涉及的规则编号
    reason: str          # 人类可读的判定依据
    example: dict        # 覆盖区间内的示例报文
    region: str = ""     # 覆盖/冲突区间描述


def analyze(rules):
    """返回 (findings, coverage)。coverage 为 (i, j) 列表，表示规则 i 完整覆盖规则 j。"""
    findings = []
    coverage = []

    # 两两覆盖关系与矛盾对
    for i in range(len(rules)):
        for j in range(i + 1, len(rules)):
            ri, rj = rules[i], rules[j]
            if covers(ri.rect, rj.rect):
                coverage.append((ri.rid, rj.rid))
            if covers(rj.rect, ri.rect):
                coverage.append((rj.rid, ri.rid))
            inter = intersection(ri.rect, rj.rect)
            if inter is not None and ri.action != rj.action:
                findings.append(Finding(
                    kind="contradiction",
                    rules=[ri.rid, rj.rid],
                    reason=(f"规则 {ri.rid}({ri.action}) 与规则 {rj.rid}({rj.action}) "
                            f"匹配空间相交；当前顺序下相交区间由规则 {ri.rid} 决定，"
                            f"交换两条规则后该区间的判定结果会反转（顺序敏感）。"),
                    example=example_packet(inter),
                    region=describe_rect(inter),
                ))

    # 逐条规则做首匹配模拟，判定冗余 / 永不命中
    for j in range(len(rules)):
        rj = rules[j]
        unclaimed = [rj.rect]
        shadowed_by_opposite = []   # 被相反动作的更早规则抢先占据的区域
        same_coverers, opp_coverers = [], []
        for i in range(j):
            if not unclaimed:
                break
            ri = rules[i]
            nxt = []
            for piece in unclaimed:
                inter = intersection(piece, ri.rect)
                if inter is None:
                    nxt.append(piece)
                    continue
                if ri.action == rj.action:
                    if ri.rid not in same_coverers:
                        same_coverers.append(ri.rid)
                else:
                    shadowed_by_opposite.append(inter)
                    if ri.rid not in opp_coverers:
                        opp_coverers.append(ri.rid)
                nxt.extend(subtract(piece, ri.rect))
            unclaimed = nxt
        if unclaimed:
            continue  # 有未被更早规则覆盖的空间，正常生效
        if shadowed_by_opposite:
            findings.append(Finding(
                kind="never_hit",
                rules=[rj.rid] + opp_coverers,
                reason=(f"规则 {rj.rid} 的全部匹配空间已被更早的相反动作规则 "
                        f"{opp_coverers} 覆盖，任何报文都会先命中那些规则，"
                        f"规则 {rj.rid} 永远不会被触发。"),
                example=example_packet(shadowed_by_opposite[0]),
                region=describe_rect(shadowed_by_opposite[0]),
            ))
        else:
            findings.append(Finding(
                kind="redundant",
                rules=[rj.rid] + same_coverers,
                reason=(f"规则 {rj.rid}({rj.action}) 的全部匹配空间已被更早的同动作规则 "
                        f"{same_coverers} 覆盖，删除它不会改变任何报文的判定结果。"),
                example=example_packet(rj.rect),
                region=describe_rect(rj.rect),
            ))
    return findings, coverage


# ---------------------------------------------------------------- 报告

def render_report(rules, findings, coverage):
    lines = []
    lines.append("# 策略分析报告")
    lines.append("")
    lines.append(f"共 {len(rules)} 条规则，检出 "
                 f"{sum(1 for f in findings if f.kind == 'redundant')} 条冗余、"
                 f"{sum(1 for f in findings if f.kind == 'contradiction')} 组矛盾、"
                 f"{sum(1 for f in findings if f.kind == 'never_hit')} 条永不命中。")
    lines.append("")

    lines.append("## 规则清单")
    lines.append("")
    for r in rules:
        lines.append(f"- 规则 {r.rid}: {r.action} {describe_rect(r.rect)}")
    lines.append("")

    lines.append("## 覆盖关系")
    lines.append("")
    if coverage:
        for a, b in coverage:
            lines.append(f"- 规则 {a} 完整覆盖规则 {b}")
    else:
        lines.append("- （无完整覆盖关系）")
    lines.append("")

    titles = {"redundant": "冗余规则", "contradiction": "矛盾规则对",
              "never_hit": "永不命中规则"}
    for kind in ("redundant", "contradiction", "never_hit"):
        lines.append(f"## {titles[kind]}")
        lines.append("")
        group = [f for f in findings if f.kind == kind]
        if not group:
            lines.append("- （无）")
        for f in group:
            lines.append(f"- 涉及规则 {f.rules}: {f.reason}")
            lines.append(f"  - 区间: {f.region}")
            lines.append(f"  - 示例报文: {f.example}")
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="访问策略规则分析")
    parser.add_argument("rules", help="规则 JSON 文件路径")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("-o", "--output", help="报告输出文件（默认打印到 stdout）")
    args = parser.parse_args(argv)

    rules = load_rules(args.rules)
    findings, coverage = analyze(rules)

    if args.json:
        out = json.dumps({
            "findings": [{"kind": f.kind, "rules": f.rules, "reason": f.reason,
                          "region": f.region, "example": f.example}
                         for f in findings],
            "coverage": coverage,
        }, ensure_ascii=False, indent=2)
    else:
        out = render_report(rules, findings, coverage)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(out + "\n")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
