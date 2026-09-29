# -*- coding: utf-8 -*-
"""对拍：mime_parser vs 标准库 email（policy.default）。

对 corpus.CORPUS 中每封报文，比较：
  1. 层级结构：每个部分的 (路径, 内容类型, 是否容器)
  2. 叶子部分解码后内容的 SHA-256 摘要（含附件）
"""
import hashlib
import sys

from email import policy
from email.parser import BytesParser

from corpus import CORPUS
from mime_parser import parse_message


def mine_tree(raw):
    root = parse_message(raw)
    rows = []
    for part in root.walk():
        digest = None if part.is_container else part.digest()
        rows.append((part.path, part.content_type, part.is_container, digest))
    return rows


def ref_tree(raw):
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    rows = []

    def visit(node, path):
        payload = node.get_payload()
        if node.is_multipart() and isinstance(payload, list):
            rows.append((path, node.get_content_type(), True, None))
            for index, sub in enumerate(payload):
                visit(sub, "%s.%d" % (path, index + 1))
        else:
            data = node.get_payload(decode=True)
            if data is None:
                data = b""
            if isinstance(data, str):
                data = data.encode("utf-8", "surrogateescape")
            rows.append((path, node.get_content_type(), False,
                         hashlib.sha256(data).hexdigest()))

    visit(msg, "1")
    return rows


def main():
    failures = 0
    for name, raw in CORPUS:
        mine, ref = mine_tree(raw), ref_tree(raw)
        if mine == ref:
            print("ok   %s (%d parts)" % (name, len(mine)))
            continue
        failures += 1
        print("FAIL %s" % name)
        print("  mine:")
        for row in mine:
            print("    %s %s container=%s digest=%s" % row)
        print("  reference (email):")
        for row in ref:
            print("    %s %s container=%s digest=%s" % row)
    total = len(CORPUS)
    print("\n%d/%d cases match reference" % (total - failures, total))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
