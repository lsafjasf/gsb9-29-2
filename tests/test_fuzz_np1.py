"""Self-tests for the structure-aware mutation framework (stdlib unittest)."""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fuzz_np1 import mutator
from fuzz_np1.minimizer import ddmin, minimize
from fuzz_np1.protocol import (
    Document,
    Node,
    TAG_BLOB,
    TAG_STRUCT,
    TAG_U32,
    build_deep,
    build_medium,
    build_small,
    clone_doc,
    serialize_doc,
    tree_depth,
)
from fuzz_np1.runner import (
    SafeRunner,
    STATUS_CRASH,
    STATUS_OK,
    STATUS_REJECT,
    STATUS_TIMEOUT,
)
from fuzz_np1.sut import ProtocolError, parse_message


class FrameworkTests(unittest.TestCase):
    def setUp(self):
        self.doc = build_small()

    # -- protocol / seeds --------------------------------------------------

    def test_valid_seeds_parse_cleanly(self):
        parse_message(serialize_doc(build_small()))
        parse_message(serialize_doc(build_medium()))
        self.assertEqual(parse_message(serialize_doc(Document())), [])

    # -- mutators ----------------------------------------------------------

    def test_len_pin_tamper(self):
        specs = mutator.enumerate_doc_specs(self.doc)
        self.assertIn(("len_pin_abs", 1, 0xFFFFFFFF), specs)
        mutated = mutator.apply_doc_spec(self.doc, ("len_pin_abs", 1, 0))
        wire = serialize_doc(mutated)
        with self.assertRaises(Exception):
            parse_message(wire)

    def test_u32_boundary_replace(self):
        mutated = mutator.apply_doc_spec(self.doc, ("u32_boundary", 0, 0))
        parsed = parse_message(serialize_doc(mutated))
        self.assertEqual(parsed[0][1], 0)

    def test_blob_pattern_replace(self):
        mutated = mutator.apply_doc_spec(self.doc, ("blob_pattern", 1, 4))
        parsed = parse_message(serialize_doc(mutated))
        self.assertTrue(parsed[1][1].startswith(b"NP"))

    def test_reorder_fields(self):
        before = [c.tag for c in self.doc.children[2].value]
        mutated = mutator.apply_doc_spec(self.doc, ("reorder", 2, "reverse"))
        after = [c.tag for c in mutated.children[2].value]
        self.assertEqual(after, list(reversed(before)))
        parse_message(serialize_doc(mutated))  # still valid

    def test_nesting_wrap_flatten_drop_insert(self):
        original_depth = tree_depth(self.doc)
        wrapped = mutator.apply_doc_spec(self.doc, ("wrap", 2, TAG_STRUCT))
        self.assertGreater(tree_depth(wrapped), original_depth)
        flat = mutator.apply_doc_spec(wrapped, ("flatten", 2, 0))
        self.assertEqual(serialize_doc(flat), serialize_doc(self.doc))
        dropped = mutator.apply_doc_spec(self.doc, ("drop", 0, 0))
        self.assertEqual(len(dropped.children), len(self.doc.children) - 1)
        inserted = mutator.apply_doc_spec(self.doc, ("insert", 0, TAG_U32))
        self.assertEqual(len(inserted.children), len(self.doc.children) + 1)

    def test_struct_count_pin(self):
        mutated = mutator.apply_doc_spec(self.doc, ("count_pin_abs", 2, 1))
        wire = serialize_doc(mutated)
        with self.assertRaises(AssertionError):
            parse_message(wire)

    def test_root_count_pin(self):
        mutated = mutator.apply_doc_spec(self.doc, ("count_pin_abs", -1, 9))
        wire = serialize_doc(mutated)
        with self.assertRaises((ProtocolError, AssertionError)):
            parse_message(wire)

    def test_inapplicable_specs_are_noop(self):
        # u32_boundary on a BLOB node is not applicable
        self.assertIsNone(
            mutator.apply_doc_spec(self.doc, ("u32_boundary", 1, 0)))

    # -- deterministic, seeded generation ----------------------------------

    def test_random_stream_is_seed_reproducible(self):
        def stream(seed):
            rng = random.Random(seed)
            return [d for d, _s in mutator.random_variants(
                self.doc, rng, 50)]

        first = stream(12345)
        second = stream(12345)
        third = stream(12345)
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        other = stream(99999)
        self.assertNotEqual(first, other)

    def test_pair_stream_deterministic_and_capped(self):
        first = [d for d, _ in mutator.pair_doc_variants(self.doc, cap=50)]
        second = [d for d, _ in mutator.pair_doc_variants(self.doc, cap=50)]
        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 50)
        full = [d for d, _ in mutator.pair_doc_variants(self.doc, cap=None)]
        self.assertGreater(len(full), len(first))

    def test_enumeration_order_stable_across_clones(self):
        specs_a = mutator.enumerate_doc_specs(build_small())
        specs_b = mutator.enumerate_doc_specs(clone_doc(build_small()))
        self.assertEqual(specs_a, specs_b)

    # -- coverage of required shapes ---------------------------------------

    def test_empty_and_header_only_samples(self):
        with self.assertRaises(ProtocolError):
            parse_message(b"")
        self.assertEqual(parse_message(serialize_doc(Document())), [])

    def test_extreme_depth_and_oversize(self):
        sys.setrecursionlimit(50000)
        deep = serialize_doc(build_deep(1500))
        with self.assertRaises(RecursionError):
            parse_message(deep)
        oversize = Document(children=[
            Node(TAG_BLOB, bytearray(b"AB"), pin_len=0xFFFFFFFF)])
        with self.assertRaises(MemoryError):
            parse_message(serialize_doc(oversize))

    def test_all_mutation_families_appear_in_enumeration(self):
        families = {spec[0] for spec in
                    mutator.enumerate_doc_specs(build_medium())}
        for family in ("len_pin_abs", "len_pin_delta", "u32_boundary",
                       "blob_pattern", "reorder", "count_pin_abs", "wrap",
                       "flatten", "drop", "insert"):
            self.assertIn(family, families)
        wire_families = {spec[0] for spec in
                         mutator.enumerate_wire_specs(
                             serialize_doc(build_medium()))}
        for family in ("truncate", "append", "duptail", "flipbyte",
                       "insertbyte"):
            self.assertIn(family, wire_families)

    # -- runner + minimiser -------------------------------------------------

    def test_runner_classifies_statuses(self):
        with SafeRunner(timeout=0.3) as runner:
            status, _cls, _ = runner.run(serialize_doc(self.doc))
            self.assertEqual(status, STATUS_OK)
            status, _cls, _ = runner.run(b"\x00\x00\x00")
            self.assertEqual(status, STATUS_REJECT)
            bad = serialize_doc(mutator.apply_doc_spec(
                self.doc, ("count_pin_abs", 2, 1)))
            status, cls, _ = runner.run(bad)
            self.assertEqual(status, STATUS_CRASH)
            self.assertEqual(cls, "length_desync")

    def test_runner_detects_timeout_and_restarts(self):
        data, _specs = mutator.sentinel_variant(build_small())
        with SafeRunner(timeout=0.2) as runner:
            status, cls, _ = runner.run(data)
            self.assertEqual(status, STATUS_TIMEOUT)
            self.assertEqual(cls, "hang_sentinel")
            # worker was replaced; ordinary input still works
            status, _cls, _ = runner.run(serialize_doc(self.doc))
            self.assertEqual(status, STATUS_OK)

    def test_minimizer_preserves_assertion_class(self):
        raw = serialize_doc(mutator.apply_doc_spec(
            self.doc, ("len_pin_abs", 2, 3)))

        def oracle(candidate):
            try:
                parse_message(candidate)
            except AssertionError:
                return True
            except Exception:
                return False
            return False

        small, calls = minimize(raw, oracle, max_tries=120)
        self.assertLess(len(small), len(raw))
        self.assertTrue(oracle(small))
        self.assertGreater(calls, 0)

    def test_ddmin_unit(self):
        self.assertEqual(ddmin(b"", lambda b: True), b"")
        result = ddmin(b"abcdef", lambda b: True, max_tries=200)
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
