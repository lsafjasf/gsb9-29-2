#!/usr/bin/env python3
"""policy_analyzer 单元测试（标准库 unittest，可直接 python3 运行）。"""

import unittest

from policy_analyzer import (
    PROTO_ALL, analyze, parse_rule, parse_rules, uncovered_witness,
)


def make(text):
    rules, errors = parse_rules(text)
    assert not errors, errors
    return rules


def kinds(findings):
    return sorted(f.kind for f in findings)


class TestParse(unittest.TestCase):
    def test_basic_fields(self):
        r = parse_rule("allow tcp 10.0.0.0/8 192.168.0.0/16 80-90", 1)
        self.assertEqual(r.action, "allow")
        self.assertEqual(r.box.protos, frozenset({"tcp"}))
        self.assertEqual((r.box.port_lo, r.box.port_hi), (80, 90))
        self.assertEqual(r.box.src_hi - r.box.src_lo + 1, 2 ** 24)

    def test_wildcards(self):
        r = parse_rule("deny any * any *", 1)
        self.assertEqual(r.box.protos, PROTO_ALL)
        self.assertEqual((r.box.src_lo, r.box.src_hi), (0, 2 ** 32 - 1))
        self.assertEqual((r.box.port_lo, r.box.port_hi), (0, 65535))

    def test_single_ip_defaults_to_32(self):
        r = parse_rule("allow tcp 10.0.0.1 any 22", 1)
        self.assertEqual(r.box.src_lo, r.box.src_hi)

    def test_comments_and_blank_lines(self):
        rules, errors = parse_rules("# 注释\n\nallow tcp any any 80  # 行尾注释\n")
        self.assertEqual(errors, [])
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].rid, 1)

    def test_bad_action_and_field_count(self):
        _, errors = parse_rules("permit tcp any any 80\nallow tcp any any\n")
        self.assertEqual(len(errors), 2)

    def test_bad_port_range(self):
        _, errors = parse_rules("allow tcp any any 90-80\n")
        self.assertEqual(len(errors), 1)


class TestCoverageMath(unittest.TestCase):
    def test_contains_and_intersection(self):
        a = parse_rule("allow tcp 10.0.0.0/8 any 1-100", 1).box
        b = parse_rule("allow tcp 10.1.0.0/16 any 50-60", 2).box
        self.assertTrue(a.contains(b))
        self.assertFalse(b.contains(a))
        self.assertEqual((a.intersection(b).port_lo, a.intersection(b).port_hi), (50, 60))

    def test_disjoint_proto_no_overlap(self):
        a = parse_rule("allow tcp any any 80", 1).box
        b = parse_rule("allow udp any any 80", 2).box
        self.assertIsNone(a.intersection(b))

    def test_uncovered_witness_none_when_covered(self):
        big = parse_rule("allow any any any any", 1).box
        small = parse_rule("deny tcp 10.0.0.0/8 any 22", 2).box
        self.assertIsNone(uncovered_witness(small, [big]))

    def test_uncovered_witness_finds_gap(self):
        a = parse_rule("allow tcp any any 80", 1).box
        b = parse_rule("allow tcp any any 80-90", 2).box
        w = uncovered_witness(b, [a])
        self.assertIsNotNone(w)
        self.assertGreaterEqual(w.port_lo, 81)


class TestAnalyze(unittest.TestCase):
    def test_single_rule_no_findings(self):
        findings = analyze(make("allow tcp any any 80\n"))
        self.assertEqual(findings, [])

    def test_no_conflict_disjoint(self):
        findings = analyze(make(
            "allow tcp 10.0.0.0/8 any 80\n"
            "deny  tcp 192.168.0.0/16 any 22\n"
            "allow udp 10.0.0.0/8 any 53\n"
        ))
        self.assertEqual(findings, [])

    def test_exact_duplicate_is_redundant(self):
        findings = analyze(make(
            "allow tcp 10.0.0.0/8 any 80\n"
            "allow tcp 10.0.0.0/8 any 80\n"
        ))
        self.assertEqual(kinds(findings), ["redundant"])
        self.assertEqual(findings[0].rule_ids, (2,))

    def test_union_cover_redundant(self):
        # 规则3 的 /15 被规则1(/24) 与规则2(/24) 联合覆盖
        findings = analyze(make(
            "allow tcp 10.0.0.0/16 any 8080\n"
            "allow tcp 10.1.0.0/16 any 8080\n"
            "allow tcp 10.0.0.0/15 any 8080\n"
        ))
        self.assertEqual(kinds(findings), ["redundant"])
        basis_text = " ".join(findings[0].basis)
        self.assertIn("规则 #1", basis_text)
        self.assertIn("规则 #2", basis_text)

    def test_partial_overlap_not_redundant(self):
        findings = analyze(make(
            "allow tcp any any 80\n"
            "allow tcp any any 80-90\n"
        ))
        self.assertEqual(findings, [])

    def test_full_wildcard_shadows_everything(self):
        findings = analyze(make(
            "allow any any any any\n"
            "deny  tcp 10.0.0.1 any 22\n"
        ))
        self.assertEqual(kinds(findings), ["conflict", "never_hit"])
        never = [f for f in findings if f.kind == "never_hit"][0]
        self.assertEqual(never.rule_ids, (2,))

    def test_conflict_pair_and_winner(self):
        findings = analyze(make(
            "deny  tcp any 192.168.1.10 22\n"
            "allow tcp 10.1.0.0/16 192.168.1.10 22\n"
        ))
        self.assertEqual(kinds(findings), ["conflict", "never_hit"])
        conflict = [f for f in findings if f.kind == "conflict"][0]
        self.assertEqual(conflict.rule_ids, (1, 2))
        self.assertIn("#1(deny) 胜出", conflict.witness)

    def test_partial_conflict_only(self):
        # 部分重叠：后面的规则仍有独立命中空间，只报矛盾不报永不命中
        findings = analyze(make(
            "deny  tcp 10.0.0.0/8 any 1-100\n"
            "allow tcp 10.0.0.0/8 any 50-200\n"
        ))
        self.assertEqual(kinds(findings), ["conflict"])
        conflict = findings[0]
        self.assertIn("port=50-100", conflict.basis[0])

    def test_order_sensitive(self):
        # 顺序一：deny 在前，allow 被遮蔽
        f1 = analyze(make(
            "deny  tcp any any 22\n"
            "allow tcp 10.0.0.0/8 any 22\n"
        ))
        self.assertEqual(kinds(f1), ["conflict", "never_hit"])
        # 顺序二：allow 在前，deny 仍有独立空间（any 去掉 10/8），无永不命中
        f2 = analyze(make(
            "allow tcp 10.0.0.0/8 any 22\n"
            "deny  tcp any any 22\n"
        ))
        self.assertEqual(kinds(f2), ["conflict"])
        # 顺序三：完全同空间，后者被遮蔽
        f3 = analyze(make(
            "allow tcp any any 22\n"
            "deny  tcp any any 22\n"
        ))
        self.assertEqual(kinds(f3), ["conflict", "never_hit"])

    def test_same_action_overlap_no_conflict(self):
        findings = analyze(make(
            "allow tcp any any 1-100\n"
            "allow tcp any any 50-200\n"
        ))
        self.assertEqual(findings, [])

    def test_protocol_wildcard_shadow(self):
        findings = analyze(make(
            "deny  any 172.16.0.0/12 any any\n"
            "allow tcp 172.16.5.0/24 any 25\n"
        ))
        self.assertEqual(kinds(findings), ["conflict", "never_hit"])

    def test_witness_is_concrete_packet(self):
        findings = analyze(make(
            "deny  tcp any any 22\n"
            "allow tcp any any 22\n"
        ))
        conflict = [f for f in findings if f.kind == "conflict"][0]
        self.assertIn("tcp 0.0.0.0 -> 0.0.0.0:22", conflict.witness)


if __name__ == "__main__":
    unittest.main(verbosity=2)
