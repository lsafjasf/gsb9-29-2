"""policy_analyzer 自测：覆盖单规则、全通配、无冲突、顺序敏感及边界用例。"""

import unittest

from policy_analyzer import (
    ALL_PROTOCOLS, Rect, analyze, covers, intersection, parse_rule,
    remaining, subtract, example_packet,
)


def mk(rid, action, src="*", dst="*", protocol="*", port="*"):
    return parse_rule({"id": rid, "action": action, "src": src, "dst": dst,
                       "protocol": protocol, "port": port})


def kinds(findings):
    return sorted(f.kind for f in findings)


class TestParsing(unittest.TestCase):
    def test_wildcard_defaults(self):
        r = parse_rule({"action": "allow"})
        self.assertEqual(r.rect.src_lo, 0)
        self.assertEqual(r.rect.src_hi, 0xFFFFFFFF)
        self.assertEqual(r.rect.port_lo, 0)
        self.assertEqual(r.rect.port_hi, 65535)
        self.assertEqual(r.rect.protos, ALL_PROTOCOLS)

    def test_cidr_and_port_range(self):
        r = mk(1, "allow", src="10.0.0.0/8", port="8000-9000", protocol="tcp")
        self.assertEqual(r.rect.src_hi - r.rect.src_lo, 0xFFFFFF)
        self.assertEqual((r.rect.port_lo, r.rect.port_hi), (8000, 9000))
        self.assertEqual(r.rect.protos, frozenset({"tcp"}))

    def test_single_host_32(self):
        r = mk(1, "deny", src="192.168.1.1/32")
        self.assertEqual(r.rect.src_lo, r.rect.src_hi)

    def test_invalid_action(self):
        with self.assertRaises(ValueError):
            parse_rule({"action": "permit"})

    def test_invalid_port_range(self):
        with self.assertRaises(ValueError):
            mk(1, "allow", port="9000-8000")
        with self.assertRaises(ValueError):
            mk(1, "allow", port="70000")

    def test_unknown_protocol(self):
        with self.assertRaises(ValueError):
            mk(1, "allow", protocol="gre")


class TestRectOps(unittest.TestCase):
    def test_covers_wildcard_everything(self):
        any_rect = mk(1, "allow").rect
        specific = mk(2, "allow", src="10.0.0.0/8", port="80", protocol="tcp").rect
        self.assertTrue(covers(any_rect, specific))
        self.assertFalse(covers(specific, any_rect))

    def test_cidr_containment(self):
        a = mk(1, "allow", src="10.0.0.0/8").rect
        b = mk(2, "allow", src="10.1.0.0/16").rect
        self.assertTrue(covers(a, b))
        self.assertFalse(covers(b, a))

    def test_adjacent_ports_do_not_overlap(self):
        a = mk(1, "allow", port="80").rect
        b = mk(2, "allow", port="81-90").rect
        self.assertIsNone(intersection(a, b))

    def test_boundary_ports(self):
        a = mk(1, "allow", port="0").rect
        b = mk(2, "allow", port="65535").rect
        self.assertIsNone(intersection(a, b))
        c = mk(3, "allow", port="0-65535").rect
        self.assertTrue(covers(c, a))
        self.assertTrue(covers(c, b))

    def test_adjacent_networks_do_not_overlap(self):
        a = mk(1, "allow", src="10.0.0.0/25").rect
        b = mk(2, "allow", src="10.0.0.128/25").rect
        self.assertIsNone(intersection(a, b))

    def test_subtract_split_by_two_rules(self):
        # 一条规则的空间被两条更早规则拼起来完全覆盖（矩形差集分裂场景）
        target = mk(3, "allow", port="100-200").rect
        left = mk(1, "allow", port="100-150").rect
        right = mk(2, "allow", port="151-200").rect
        self.assertEqual(remaining(target, [left, right]), [])

    def test_subtract_leaves_remainder(self):
        target = mk(2, "allow", port="100-200").rect
        left = mk(1, "allow", port="100-150").rect
        rest = remaining(target, [left])
        self.assertEqual(len(rest), 1)
        self.assertEqual((rest[0].port_lo, rest[0].port_hi), (151, 200))

    def test_protocol_dimension(self):
        tcp_only = mk(1, "allow", protocol="tcp").rect
        any_proto = mk(2, "allow", protocol="*").rect
        rest = remaining(any_proto, [tcp_only])
        self.assertEqual(len(rest), 1)
        self.assertEqual(rest[0].protos, frozenset({"udp", "icmp"}))


class TestAnalyze(unittest.TestCase):
    def test_single_rule_no_findings(self):
        findings, coverage = analyze([mk(1, "allow", src="10.0.0.0/8")])
        self.assertEqual(findings, [])
        self.assertEqual(coverage, [])

    def test_single_full_wildcard_no_findings(self):
        findings, _ = analyze([mk(1, "allow")])
        self.assertEqual(findings, [])

    def test_disjoint_rules_no_conflict(self):
        rules = [
            mk(1, "allow", src="10.0.0.0/8", port="80", protocol="tcp"),
            mk(2, "deny", src="172.16.0.0/12", port="443", protocol="tcp"),
            mk(3, "allow", src="192.168.0.0/16", protocol="udp"),
        ]
        findings, coverage = analyze(rules)
        self.assertEqual(findings, [])
        self.assertEqual(coverage, [])

    def test_identical_rules_second_redundant(self):
        rules = [mk(1, "allow", src="10.0.0.0/8", port="80", protocol="tcp"),
                 mk(2, "allow", src="10.0.0.0/8", port="80", protocol="tcp")]
        findings, _ = analyze(rules)
        self.assertEqual(kinds(findings), ["redundant"])
        self.assertEqual(findings[0].rules, [2, 1])
        self.assertEqual(findings[0].example,
                         {"src": "10.0.0.0", "dst": "0.0.0.0",
                          "protocol": "tcp", "port": 80})

    def test_redundant_by_union_of_two_rules(self):
        rules = [
            mk(1, "allow", port="100-150"),
            mk(2, "allow", port="151-200"),
            mk(3, "allow", port="100-200"),
        ]
        findings, _ = analyze(rules)
        self.assertEqual(kinds(findings), ["redundant"])
        self.assertEqual(findings[0].rules[0], 3)
        self.assertEqual(set(findings[0].rules[1:]), {1, 2})

    def test_contradiction_order_sensitive(self):
        # deny 在前：deny 生效，allow 的重叠部分被遮蔽
        rules = [
            mk(1, "deny", src="10.0.0.0/8"),
            mk(2, "allow", src="10.1.0.0/16"),
        ]
        findings, _ = analyze(rules)
        self.assertEqual(kinds(findings), ["contradiction", "never_hit"])
        contra = next(f for f in findings if f.kind == "contradiction")
        self.assertEqual(contra.rules, [1, 2])
        self.assertEqual(contra.example["src"], "10.1.0.0")
        never = next(f for f in findings if f.kind == "never_hit")
        self.assertEqual(never.rules[0], 2)

    def test_contradiction_reverse_order_shadows_deny(self):
        # allow 在前：deny 被完全遮蔽（顺序敏感，交换后判定反转）
        rules = [
            mk(1, "allow", src="10.0.0.0/8"),
            mk(2, "deny", src="10.1.0.0/16"),
        ]
        findings, _ = analyze(rules)
        self.assertEqual(kinds(findings), ["contradiction", "never_hit"])
        contra = next(f for f in findings if f.kind == "contradiction")
        self.assertIn("顺序敏感", contra.reason)

    def test_contradiction_partial_overlap_both_effective(self):
        # 部分相交：两条规则都有各自的生效空间，仅重叠区间顺序敏感
        rules = [
            mk(1, "allow", src="10.0.0.0/8", port="80"),
            mk(2, "deny", src="10.1.0.0/16", port="80-90"),
        ]
        findings, _ = analyze(rules)
        self.assertEqual(kinds(findings), ["contradiction"])

    def test_allow_all_then_deny_never_hit(self):
        rules = [
            mk(1, "allow"),
            mk(2, "deny", src="172.16.0.0/12", port="53", protocol="udp"),
        ]
        findings, coverage = analyze(rules)
        self.assertEqual(kinds(findings), ["contradiction", "never_hit"])
        self.assertIn((1, 2), coverage)

    def test_allow_all_then_allow_redundant(self):
        rules = [
            mk(1, "allow"),
            mk(2, "allow", src="192.0.2.0/24", port="443", protocol="tcp"),
        ]
        findings, _ = analyze(rules)
        self.assertEqual(kinds(findings), ["redundant"])
        self.assertEqual(findings[0].rules, [2, 1])

    def test_partial_overlap_not_never_hit(self):
        # 只有部分空间被遮蔽：报矛盾但不报永不命中
        rules = [
            mk(1, "deny", src="10.0.0.0/9"),
            mk(2, "allow", src="10.0.0.0/8"),
        ]
        findings, _ = analyze(rules)
        self.assertEqual(kinds(findings), ["contradiction"])

    def test_example_packet_inside_conflict_region(self):
        rules = [
            mk(1, "allow", src="10.0.0.0/8", port="80-100", protocol="tcp"),
            mk(2, "deny", src="10.1.0.0/16", port="90-200", protocol="tcp"),
        ]
        findings, _ = analyze(rules)
        contra = next(f for f in findings if f.kind == "contradiction")
        ex = contra.example
        self.assertTrue(ex["src"].startswith("10.1."))
        self.assertTrue(90 <= ex["port"] <= 100)
        self.assertEqual(ex["protocol"], "tcp")

    def test_different_protocols_no_conflict(self):
        rules = [
            mk(1, "allow", src="10.0.0.0/8", protocol="tcp"),
            mk(2, "deny", src="10.0.0.0/8", protocol="udp"),
        ]
        findings, _ = analyze(rules)
        self.assertEqual(findings, [])

    def test_dst_dimension_isolation(self):
        rules = [
            mk(1, "allow", src="10.0.0.0/8", dst="192.168.0.0/16"),
            mk(2, "deny", src="10.0.0.0/8", dst="172.16.0.0/12"),
        ]
        findings, _ = analyze(rules)
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
