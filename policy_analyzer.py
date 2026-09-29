#!/usr/bin/env python3
"""访问策略规则分析库（仅使用 Python 标准库）。

功能：
  - 解析 allow/deny 规则（网段、端口范围、协议、通配符）。
  - 计算规则间的覆盖关系（多维区间包含 / 相交）。
  - 检出三类问题并给出可解释依据：
      1. redundant  完全被先前同动作规则覆盖的冗余规则；
      2. conflict   动作相反且匹配空间重叠的矛盾规则对；
      3. never_hit  所有报文均被先前规则决定、永远不会命中的规则。

规则语义：按编号从小到大依次匹配，先命中先生效（顺序敏感）。

规则文本格式（每行一条，# 之后为注释）：
    <allow|deny> <proto> <src网段> <dst网段> <端口>
示例：
    allow tcp 10.0.0.0/8 any 80
    deny  any 0.0.0.0/0 192.168.1.0/24 1-1024
其中 proto 可取 tcp/udp/icmp/*（any），网段与端口可用 * 或 any 表示通配。
"""

import argparse
import ipaddress
import sys
from dataclasses import dataclass, replace

PROTO_ALL = frozenset({"tcp", "udp", "icmp"})
PORT_MIN, PORT_MAX = 0, 65535
IP_LO, IP_HI = 0, 2 ** 32 - 1  # 仅支持 IPv4

_DIMS = ("src", "dst", "port")


# ---------------------------------------------------------------------------
# 匹配空间：一条规则的匹配空间是 (源网段 × 目的网段 × 协议集合 × 端口区间)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Box:
    src_lo: int
    src_hi: int
    dst_lo: int
    dst_hi: int
    protos: frozenset
    port_lo: int
    port_hi: int

    def _range(self, dim):
        return (getattr(self, dim + "_lo"), getattr(self, dim + "_hi"))

    def with_range(self, dim, lo, hi):
        return replace(self, **{dim + "_lo": lo, dim + "_hi": hi})

    def contains(self, other):
        """self 是否完整包含 other（四个维度全部包含）。"""
        return (
            self.src_lo <= other.src_lo and other.src_hi <= self.src_hi
            and self.dst_lo <= other.dst_lo and other.dst_hi <= self.dst_hi
            and self.protos >= other.protos
            and self.port_lo <= other.port_lo and other.port_hi <= self.port_hi
        )

    def overlaps(self, other):
        return self.intersection(other) is not None

    def intersection(self, other):
        protos = self.protos & other.protos
        box = Box(
            max(self.src_lo, other.src_lo), min(self.src_hi, other.src_hi),
            max(self.dst_lo, other.dst_lo), min(self.dst_hi, other.dst_hi),
            protos,
            max(self.port_lo, other.port_lo), min(self.port_hi, other.port_hi),
        )
        if box.src_lo > box.src_hi or box.dst_lo > box.dst_hi \
                or box.port_lo > box.port_hi or not protos:
            return None
        return box


def _fmt_ip(value):
    return str(ipaddress.IPv4Address(value))


def _fmt_ip_range(lo, hi):
    if lo == IP_LO and hi == IP_HI:
        return "any"
    if lo == hi:
        return _fmt_ip(lo)
    return "%s-%s" % (_fmt_ip(lo), _fmt_ip(hi))


def _fmt_protos(protos):
    if protos == PROTO_ALL:
        return "any"
    return "/".join(sorted(protos))


def _fmt_port_range(lo, hi):
    if lo == PORT_MIN and hi == PORT_MAX:
        return "any"
    if lo == hi:
        return str(lo)
    return "%d-%d" % (lo, hi)


def describe_box(box):
    return "src=%s dst=%s proto=%s port=%s" % (
        _fmt_ip_range(box.src_lo, box.src_hi),
        _fmt_ip_range(box.dst_lo, box.dst_hi),
        _fmt_protos(box.protos),
        _fmt_port_range(box.port_lo, box.port_hi),
    )


def sample_packet(box):
    """给出空间内一个具体报文示例（取各维度下界）。"""
    proto = sorted(box.protos)[0]
    return "%s %s -> %s:%d" % (
        proto, _fmt_ip(box.src_lo), _fmt_ip(box.dst_lo), box.port_lo)


# ---------------------------------------------------------------------------
# 覆盖判定：box 是否被一组空间（规则的并集）完全覆盖
# ---------------------------------------------------------------------------

def _atomize(box, others):
    """把 box 按 others 的边界切成原子小格（每格在任意 other 内/外状态一致）。"""
    boxes = [box]
    for dim in _DIMS:
        out = []
        for b in boxes:
            lo, hi = b._range(dim)
            cuts = {lo, hi + 1}
            for o in others:
                olo, ohi = o._range(dim)
                cuts.add(min(max(olo, lo), hi + 1))
                cuts.add(min(max(ohi + 1, lo), hi + 1))
            pts = sorted(cuts)
            for i in range(len(pts) - 1):
                if pts[i] <= pts[i + 1] - 1:
                    out.append(b.with_range(dim, pts[i], pts[i + 1] - 1))
        boxes = out
    out = []
    for b in boxes:
        for p in sorted(b.protos):
            out.append(replace(b, protos=frozenset({p})))
    return out


def _try_merge(a, b):
    """若两个 Box 仅在一个维度上相邻/可并，返回合并结果，否则 None。"""
    if a.protos == b.protos:
        diffs = [d for d in _DIMS if a._range(d) != b._range(d)]
        if len(diffs) == 1:
            d = diffs[0]
            alo, ahi = a._range(d)
            blo, bhi = b._range(d)
            if ahi + 1 == blo:
                return a.with_range(d, alo, bhi)
            if bhi + 1 == alo:
                return a.with_range(d, blo, ahi)
        if not diffs:
            return a
    # 范围维度完全一致时合并协议集合
    if all(a._range(d) == b._range(d) for d in _DIMS):
        return replace(a, protos=a.protos | b.protos)
    return None


def _merge_boxes(boxes):
    boxes = list(boxes)
    changed = True
    while changed:
        changed = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                merged = _try_merge(boxes[i], boxes[j])
                if merged is not None:
                    boxes[i] = merged
                    del boxes[j]
                    changed = True
                    break
            if changed:
                break
    return boxes


def uncovered_witness(box, others):
    """若 box 未被 others 完全覆盖，返回一个未覆盖子空间；否则返回 None。"""
    atoms = _atomize(box, others)
    for atom in atoms:
        if not any(o.contains(atom) for o in others):
            return atom
    return None


def cover_basis(box, others):
    """若 box 被 others 完全覆盖，返回覆盖依据 [(other下标, 该规则覆盖的区间)]；
    否则返回 None。采用贪心集合覆盖，区间做相邻合并以便阅读。"""
    atoms = _atomize(box, others)
    cand = []
    for atom in atoms:
        c = [i for i, o in enumerate(others) if o.contains(atom)]
        if not c:
            return None
        cand.append(c)
    remaining = set(range(len(atoms)))
    chosen = {}
    while remaining:
        pool = set()
        for idx in remaining:
            pool.update(cand[idx])
        best = max(pool, key=lambda r: sum(1 for a in remaining if r in cand[a]))
        chosen.setdefault(best, []).extend(
            atoms[a] for a in sorted(remaining) if best in cand[a])
        remaining = {a for a in remaining if best not in cand[a]}
    return [(idx, _merge_boxes(boxes)) for idx, boxes in sorted(chosen.items())]


# ---------------------------------------------------------------------------
# 规则解析
# ---------------------------------------------------------------------------

@dataclass
class Rule:
    rid: int          # 规则编号（1 起，按文件顺序）
    action: str       # 'allow' 或 'deny'
    box: Box
    raw: str          # 原始文本（去注释）

    def __str__(self):
        return "#%d %s %s" % (self.rid, self.action, describe_box(self.box))


def _parse_proto(token):
    token = token.lower()
    if token in ("*", "any"):
        return PROTO_ALL
    if token in PROTO_ALL:
        return frozenset({token})
    raise ValueError("未知协议: %r（支持 tcp/udp/icmp/*/any）" % token)


def _parse_network(token):
    token = token.strip()
    if token.lower() in ("*", "any"):
        return IP_LO, IP_HI
    if "/" not in token:
        token += "/32"
    try:
        net = ipaddress.IPv4Network(token, strict=False)
    except ValueError as exc:
        raise ValueError("非法网段: %r (%s)" % (token, exc))
    return int(net.network_address), int(net.broadcast_address)


def _parse_port(token):
    token = token.strip()
    if token.lower() in ("*", "any"):
        return PORT_MIN, PORT_MAX
    if "-" in token:
        lo_s, hi_s = token.split("-", 1)
        lo, hi = int(lo_s), int(hi_s)
    else:
        lo = hi = int(token)
    if not (PORT_MIN <= lo <= hi <= PORT_MAX):
        raise ValueError("非法端口范围: %r" % token)
    return lo, hi


def parse_rule(line, rid):
    """解析单行规则文本，返回 Rule。格式错误抛 ValueError。"""
    raw = line.split("#", 1)[0].strip()
    tokens = raw.split()
    if len(tokens) != 5:
        raise ValueError("规则 #%d 应有 5 个字段 <动作 协议 源网段 目的网段 端口>，实际 %d 个: %r"
                         % (rid, len(tokens), raw))
    action = tokens[0].lower()
    if action not in ("allow", "deny"):
        raise ValueError("规则 #%d 动作须为 allow/deny: %r" % (rid, tokens[0]))
    protos = _parse_proto(tokens[1])
    src_lo, src_hi = _parse_network(tokens[2])
    dst_lo, dst_hi = _parse_network(tokens[3])
    port_lo, port_hi = _parse_port(tokens[4])
    return Rule(rid, action,
                Box(src_lo, src_hi, dst_lo, dst_hi, protos, port_lo, port_hi),
                raw)


def parse_rules(text):
    """解析多行规则文本，跳过空行与纯注释行。返回 (rules, errors)。"""
    rules, errors = [], []
    rid = 0
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        rid += 1
        try:
            rules.append(parse_rule(line, rid))
        except ValueError as exc:
            errors.append("第 %d 行: %s" % (lineno, exc))
    return rules, errors


# ---------------------------------------------------------------------------
# 分析
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    kind: str                 # 'redundant' | 'conflict' | 'never_hit'
    rule_ids: tuple           # 涉及的规则编号
    summary: str              # 一句话结论
    basis: tuple              # 判定依据（多行文本）
    witness: str              # 覆盖/重叠区间示例


def _basis_lines(rule, basis, rules_by_index):
    lines = []
    for idx, boxes in basis:
        covering = rules_by_index[idx]
        for b in boxes:
            lines.append("规则 #%d(%s) 覆盖区间: %s"
                         % (covering.rid, covering.action, describe_box(b)))
    return tuple(lines)


def analyze(rules):
    """对规则列表（按生效顺序）进行分析，返回 Finding 列表。"""
    findings = []
    boxes = [r.box for r in rules]

    for j, rule in enumerate(rules):
        earlier = rules[:j]
        if not earlier:
            continue
        earlier_boxes = boxes[:j]
        same_idx = [i for i, r in enumerate(earlier) if r.action == rule.action]

        # 1) 冗余：被先前同动作规则（的并集）完全覆盖
        if same_idx:
            basis = cover_basis(rule.box, [earlier_boxes[i] for i in same_idx])
            if basis is not None:
                basis = [(same_idx[i], b) for i, b in basis]
                findings.append(Finding(
                    "redundant", (rule.rid,),
                    "规则 #%d(%s) 被先前同动作规则完全覆盖，删除不改变策略行为"
                    % (rule.rid, rule.action),
                    tuple(_basis_lines(rule, basis, rules)),
                    "示例报文 %s 先命中覆盖规则，动作相同"
                    % sample_packet(rule.box),
                ))
                continue  # 冗余已蕴含永不命中，不重复报告

        # 2) 永不命中：被先前规则（不限动作）完全覆盖
        basis = cover_basis(rule.box, earlier_boxes)
        if basis is not None:
            findings.append(Finding(
                "never_hit", (rule.rid,),
                "规则 #%d(%s) 的所有报文均被先前规则先行决定，永远不会命中"
                % (rule.rid, rule.action),
                tuple(_basis_lines(rule, basis, rules)),
                "示例报文 %s 命中先前规则，规则 #%d 无机会生效"
                % (sample_packet(rule.box), rule.rid),
            ))

    # 3) 矛盾：动作相反且匹配空间重叠（按顺序，先命中者生效）
    for i in range(len(rules)):
        for j in range(i + 1, len(rules)):
            a, b = rules[i], rules[j]
            if a.action == b.action:
                continue
            ov = a.box.intersection(b.box)
            if ov is None:
                continue
            findings.append(Finding(
                "conflict", (a.rid, b.rid),
                "规则 #%d(%s) 与 #%d(%s) 动作相反且匹配空间重叠"
                % (a.rid, a.action, b.rid, b.action),
                ("重叠区间: %s" % describe_box(ov),),
                "示例报文 %s 同时匹配两条规则，先生效的 #%d(%s) 胜出；"
                "若期望 #%d 生效需将其前移"
                % (sample_packet(ov), a.rid, a.action, b.rid),
            ))
    return findings


# ---------------------------------------------------------------------------
# 报告与 CLI
# ---------------------------------------------------------------------------

_KIND_TITLE = {
    "redundant": "冗余规则（完全被先前同动作规则覆盖）",
    "conflict": "矛盾规则（动作相反且空间重叠）",
    "never_hit": "永不命中规则（被先前规则完全遮蔽）",
}


def render_report(rules, findings):
    lines = []
    lines.append("策略分析报告")
    lines.append("=" * 60)
    n_allow = sum(1 for r in rules if r.action == "allow")
    lines.append("规则总数: %d (allow=%d, deny=%d)" % (len(rules), n_allow, len(rules) - n_allow))
    lines.append("")
    lines.append("规则清单:")
    for r in rules:
        lines.append("  %s" % r)
    lines.append("")
    if not findings:
        lines.append("未发现问题：无冗余、无矛盾、无永不命中规则。")
        return "\n".join(lines)
    counts = {k: 0 for k in _KIND_TITLE}
    for f in findings:
        counts[f.kind] += 1
    lines.append("问题汇总: 冗余 %d 条, 矛盾 %d 组, 永不命中 %d 条"
                 % (counts["redundant"], counts["conflict"], counts["never_hit"]))
    for kind, title in _KIND_TITLE.items():
        group = [f for f in findings if f.kind == kind]
        if not group:
            continue
        lines.append("")
        lines.append("[%s]" % title)
        for f in group:
            lines.append("  * %s" % f.summary)
            for b in f.basis:
                lines.append("      依据: %s" % b)
            lines.append("      %s" % f.witness)
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="访问策略规则分析（冗余/矛盾/永不命中）")
    parser.add_argument("rules_file", help="规则文件路径（文本格式，见模块 docstring）")
    args = parser.parse_args(argv)
    with open(args.rules_file, "r", encoding="utf-8") as fh:
        text = fh.read()
    rules, errors = parse_rules(text)
    for err in errors:
        print("解析错误: %s" % err, file=sys.stderr)
    if errors:
        return 2
    findings = analyze(rules)
    print(render_report(rules, findings))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
