"""Tests for ns_parser: unit cases, error locations, and differential
testing against xml.etree.ElementTree as the reference implementation."""

import io
import os
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ns_parser import NsParseError, parse_events

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
ERROR_DIR = os.path.join(DATA_DIR, "errors")


def reference_events(text):
    """Reference implementation: ElementTree start/end events with
    Clark-notation expanded names."""
    events = []
    for event, elem in ET.iterparse(io.StringIO(text), events=("start", "end")):
        if event == "start":
            events.append(("start", elem.tag, dict(elem.attrib)))
        else:
            events.append(("end", elem.tag))
    return events


def structure_events(text):
    """Our parser, reduced to the same shape as reference_events."""
    return [e for e in parse_events(text) if e[0] in ("start", "end")]


class DifferentialTest(unittest.TestCase):
    """Every well-formed document in data/ must produce exactly the same
    expanded-name event sequence as the reference implementation."""

    def test_diff_all_documents(self):
        files = sorted(
            f for f in os.listdir(DATA_DIR)
            if f.endswith(".xml") and os.path.isfile(os.path.join(DATA_DIR, f))
        )
        self.assertTrue(files, "no diff-test documents found")
        for name in files:
            with self.subTest(document=name):
                with open(os.path.join(DATA_DIR, name), encoding="utf-8") as fh:
                    text = fh.read()
                self.assertEqual(reference_events(text), structure_events(text))


class NamespaceRulesTest(unittest.TestCase):
    def qnames(self, text):
        return [e[1] for e in parse_events(text) if e[0] == "start"]

    def test_no_namespace(self):
        text = '<root a="1"><child/></root>'
        self.assertEqual(self.qnames(text), ["root", "child"])
        events = parse_events(text)
        self.assertEqual(events[0], ("start", "root", {"a": "1"}))

    def test_default_namespace_applies_to_elements(self):
        text = '<root xmlns="urn:d"><child/></root>'
        self.assertEqual(self.qnames(text), ["{urn:d}root", "{urn:d}child"])

    def test_default_namespace_does_not_apply_to_attributes(self):
        text = '<root xmlns="urn:d" attr="v"/>'
        events = parse_events(text)
        self.assertEqual(events[0], ("start", "{urn:d}root", {"attr": "v"}))

    def test_default_namespace_undeclaration(self):
        text = '<r xmlns="urn:d"><c xmlns=""><g/></c></r>'
        self.assertEqual(
            self.qnames(text), ["{urn:d}r", "c", "g"]
        )

    def test_prefix_redefinition_shadows_outer_binding(self):
        text = (
            '<a xmlns:p="urn:one">'
            '<p:x><b xmlns:p="urn:two"><p:y/></b><p:z/></p:x>'
            "</a>"
        )
        self.assertEqual(
            self.qnames(text),
            ["a", "{urn:one}x", "b", "{urn:two}y", "{urn:one}z"],
        )

    def test_redefinition_does_not_leak_to_siblings(self):
        text = (
            '<r xmlns:p="urn:outer">'
            '<a xmlns:p="urn:inner"><p:x/></a>'
            "<b><p:y/></b>"
            "</r>"
        )
        self.assertEqual(
            self.qnames(text),
            ["r", "a", "{urn:inner}x", "b", "{urn:outer}y"],
        )

    def test_prefixed_attribute_uses_its_own_binding(self):
        text = '<r xmlns:a="urn:1" xmlns:b="urn:2"><e a:k="1" b:k="2"/></r>'
        events = parse_events(text)
        self.assertEqual(
            events[1],
            ("start", "e", {"{urn:1}k": "1", "{urn:2}k": "2"}),
        )

    def test_attribute_prefix_redefined_on_same_element(self):
        text = '<e xmlns:p="urn:old"><f xmlns:p="urn:new" p:a="v"/></e>'
        events = parse_events(text)
        self.assertEqual(events[1], ("start", "f", {"{urn:new}a": "v"}))

    def test_xml_prefix_is_predeclared(self):
        text = '<e xml:lang="en"/>'
        events = parse_events(text)
        self.assertEqual(
            events[0][2],
            {"{http://www.w3.org/XML/1998/namespace}lang": "en"},
        )

    def test_declaration_on_same_element_is_in_scope(self):
        text = '<p:e xmlns:p="urn:late" p:a="1"/>'
        self.assertEqual(self.qnames(text), ["{urn:late}e"])
        events = parse_events(text)
        self.assertEqual(events[0][2], {"{urn:late}a": "1"})


class ErrorLocationTest(unittest.TestCase):
    def assert_error(self, text, line, column, needle="undeclared"):
        with self.assertRaises(NsParseError) as ctx:
            parse_events(text)
        err = ctx.exception
        self.assertIn(needle, err.message)
        self.assertEqual((err.line, err.column), (line, column))
        self.assertIn(f"line {line}, column {column}", str(err))

    def test_undeclared_element_prefix_position(self):
        text = "<root>\n  <ok/>\n  <oops:child/>\n</root>\n"
        # '<oops:child/>' is on line 3; the name starts at column 4.
        self.assert_error(text, line=3, column=4)

    def test_undeclared_attribute_prefix_position(self):
        text = '<root>\n  <item good="1" bad:nope="2"/>\n</root>\n'
        # 'bad:nope' starts at column 18 on line 2.
        self.assert_error(text, line=2, column=18)

    def test_undeclared_prefix_in_end_tag_of_mismatched_tree(self):
        text = "<a><p:x></p:x></a>"
        self.assert_error(text, line=1, column=5)

    def test_duplicate_expanded_attributes_rejected(self):
        text = '<r xmlns:a="urn:s" xmlns:b="urn:s"><e a:x="1" b:x="2"/></r>'
        with self.assertRaises(NsParseError) as ctx:
            parse_events(text)
        self.assertIn("same name", ctx.exception.message)

    def test_xmlns_prefix_cannot_be_declared(self):
        with self.assertRaises(NsParseError):
            parse_events('<r xmlns:xmlns="urn:x"/>')

    def test_error_files_match_reference_behaviour(self):
        # undeclared_prefix / undeclared_attr_prefix must also fail in the
        # reference implementation; ours must carry a usable position.
        for name in ("undeclared_prefix.xml", "undeclared_attr_prefix.xml"):
            with self.subTest(document=name):
                with open(os.path.join(ERROR_DIR, name), encoding="utf-8") as fh:
                    text = fh.read()
                with self.assertRaises(ET.ParseError):
                    reference_events(text)
                with self.assertRaises(NsParseError) as ctx:
                    parse_events(text)
                self.assertGreaterEqual(ctx.exception.line, 1)
                self.assertGreaterEqual(ctx.exception.column, 1)


if __name__ == "__main__":
    unittest.main()
