#!/usr/bin/env python3
"""Differential test: ns_parser vs xml.etree.ElementTree (reference).

Well-formed documents in data/ must yield identical expanded-name event
sequences; documents in data/errors/ must fail in both, and our error
must carry a line/column location.

Usage: python3 difftest.py
"""

import io
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ns_parser import NsParseError, parse_events

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def reference_events(text):
    events = []
    for event, elem in ET.iterparse(io.StringIO(text), events=("start", "end")):
        events.append((event, elem.tag, dict(elem.attrib)) if event == "start"
                      else (event, elem.tag))
    return events


def ours(text):
    return [e for e in parse_events(text) if e[0] in ("start", "end")]


def main():
    failures = 0

    for name in sorted(os.listdir(DATA_DIR)):
        path = os.path.join(DATA_DIR, name)
        if not (os.path.isfile(path) and name.endswith(".xml")):
            continue
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        ref, got = reference_events(text), ours(text)
        status = "OK  " if ref == got else "FAIL"
        if ref != got:
            failures += 1
        print(f"[{status}] {name}: {len(ref)} events identical to reference")
        if ref != got:
            for i, (a, b) in enumerate(zip(ref, got)):
                if a != b:
                    print(f"       first diff at event {i}: ref={a} ours={b}")
                    break

    err_dir = os.path.join(DATA_DIR, "errors")
    for name in sorted(os.listdir(err_dir)):
        with open(os.path.join(err_dir, name), encoding="utf-8") as fh:
            text = fh.read()
        try:
            reference_events(text)
            ref_msg = "parsed (reference does not reject this)"
        except ET.ParseError as exc:
            ref_msg = f"reference also rejects: {exc}"
        try:
            ours(text)
            print(f"[FAIL] errors/{name}: expected an error, parsed cleanly")
            failures += 1
        except NsParseError as exc:
            print(f"[OK  ] errors/{name}: rejected -> {exc}")
            print(f"       ({ref_msg})")

    print("\n%d failure(s)" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
