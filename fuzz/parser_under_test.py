#!/usr/bin/env python3
"""被测解析器：CAFE-TLV 协议。

退出码约定：
  0   解析成功
  10  头部校验失败（浅层分支）
  11  字段解析失败（中层分支）
  12  语义校验失败（深层分支）
  其他（1 / 负值信号）  未处理异常 -> 视为崩溃

默认开启 4 个有意植入的缺陷（--hardened 关闭）：
  BUG-1  字段头直接索引，无边界检查        -> IndexError（长度篡改触发）
  BUG-2  容器递归解析无深度限制            -> RecursionError（深度极大触发）
  BUG-3  RAW 长度 >= 0xFFF0 时 O(n^2) 校验 -> 超时（边界值触发）
  BUG-4  flags&0x01 时断言首字段为 INT     -> AssertionError/IndexError（重排触发）
"""

import sys

MAGIC = b"\xca\xfe"
VERSION = 1
HEADER_LEN = 6
MAX_DEPTH = 64
MAX_MESSAGE = 1 << 20

TAG_INT = 0x10
TAG_STR = 0x11
TAG_CONTAINER = 0x12
TAG_RAW = 0x13

SLOW_RAW_THRESHOLD = 0xFFF0

EXIT_OK = 0
EXIT_HEADER = 10
EXIT_FIELD = 11
EXIT_SEMANTIC = 12


def reject(code, msg):
    print(f"reject: {msg}", file=sys.stderr)
    sys.exit(code)


def slow_checksum(value):
    acc = 0
    n = len(value)
    for i in range(n):
        vi = value[i]
        for j in range(i, n):
            acc ^= vi & value[j]
    return acc


def parse_fields(data, pos, end, hardened, depth, stats):
    if hardened and depth > MAX_DEPTH:
        reject(EXIT_FIELD, "nesting too deep")
    fields = []
    while pos < end:
        if hardened and pos + 3 > end:
            reject(EXIT_FIELD, "truncated field header")
        # BUG-1: 直接索引，未检查 pos+2 < end
        tag = data[pos]
        length = (data[pos + 1] << 8) | data[pos + 2]
        if hardened and pos + 3 + length > end:
            reject(EXIT_FIELD, "field overruns parent")
        value = data[pos + 3:pos + 3 + length]
        if tag == TAG_CONTAINER:
            # BUG-2: 无递归深度限制
            value = parse_fields(value, 0, len(value), hardened, depth + 1, stats)
        elif tag == TAG_RAW:
            if not hardened and length >= SLOW_RAW_THRESHOLD:
                # BUG-3: O(n^2) 完整性校验
                slow_checksum(value)
        elif tag == TAG_INT:
            if hardened and length != 4:
                reject(EXIT_FIELD, "bad int length")
        stats["fields"] += 1
        stats["max_depth"] = max(stats["max_depth"], depth)
        fields.append((tag, value))
        pos += 3 + length
    return fields


def semantic_check(fields, flags, hardened):
    if flags & 0x01:
        if hardened:
            if not fields or fields[0][0] != TAG_INT:
                reject(EXIT_SEMANTIC, "missing length table")
        else:
            # BUG-4: 盲目断言首字段一定是 INT 长度表
            assert fields[0][0] == TAG_INT, "first field must be length table"


def parse_message(data, hardened):
    if len(data) < HEADER_LEN:
        reject(EXIT_HEADER, "short header")
    if data[:2] != MAGIC:
        reject(EXIT_HEADER, "bad magic")
    version = data[2]
    flags = data[3]
    total = (data[4] << 8) | data[5]
    if version != VERSION:
        reject(EXIT_HEADER, "bad version")
    if hardened:
        if total != len(data):
            reject(EXIT_HEADER, "total length mismatch")
        if len(data) > MAX_MESSAGE:
            reject(EXIT_HEADER, "message too large")
        body = data[HEADER_LEN:]
    else:
        body = data[HEADER_LEN:total] if total >= HEADER_LEN else b""
    stats = {"fields": 0, "max_depth": 0}
    fields = parse_fields(body, 0, len(body), hardened, 0, stats)
    semantic_check(fields, flags, hardened)
    return stats


def main(argv):
    hardened = "--hardened" in argv
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1:
        print("usage: parser_under_test.py [--hardened] FILE", file=sys.stderr)
        return 2
    with open(args[0], "rb") as fh:
        data = fh.read()
    stats = parse_message(data, hardened)
    print(f"OK fields={stats['fields']} max_depth={stats['max_depth']}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
