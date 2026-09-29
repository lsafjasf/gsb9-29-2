"""测试共享工具：帧构造、定向语料、随机语料。"""

import random

MAGIC = 0xAA
TYPES = {0x01: "PING", 0x02: "PONG", 0x03: "DATA", 0x04: "ACK"}
MAX_EXT = 1024


def build_frame(type_byte, payload, ext=False, checksum_delta=0):
    out = bytearray([MAGIC, type_byte])
    if ext:
        out.append(0xFF)
        out.append((len(payload) >> 8) & 0xFF)
        out.append(len(payload) & 0xFF)
    else:
        out.append(len(payload))
    out.extend(payload)
    cks = 0
    for b in out[1:]:
        cks ^= b
    out.append(cks ^ checksum_delta)
    return bytes(out)


def directed_streams():
    """覆盖每条迁移路径的定向字节流。"""
    ping = build_frame(0x01, b"")
    data3 = build_frame(0x03, b"abc")
    ext0 = build_frame(0x04, b"", ext=True)
    ext_big_ok = build_frame(0x03, bytes(MAX_EXT), ext=True)
    streams = [
        b"",                                        # 空输入，WAIT_MAGIC/EOF
        b"\x00\x01\x02",                            # 连续 bad_magic
        ping,                                       # 零长度帧
        data3,                                      # 短负载帧
        ext0,                                       # 扩展长度 0
        ext_big_ok,                                 # 扩展长度上限 1024
        b"\xaa\x03\xff\x04\x01" + b"\x99" * 3,      # 扩展长度 1025 -> too_large -> RESYNC/EOF
        b"\xaa\x09\x00\x00",                        # 未知类型 -> RESYNC，遇 0xAA 恢复
        b"\xaa\x09\xaa\x01\x00" + bytes([0x01]),    # 未知类型后紧跟合法帧
        build_frame(0x02, b"xy", checksum_delta=0xFF),  # 校验和错误 -> RESYNC/EOF
        build_frame(0x02, b"xy", checksum_delta=0xFF) + ping,  # 错帧后恢复出好帧
        b"\xaa",                                    # 截断于 READ_TYPE
        b"\xaa\x01",                                # 截断于 READ_LEN
        b"\xaa\x03\xff",                            # 截断于 READ_EXT_HI
        b"\xaa\x03\xff\x00",                        # 截断于 READ_EXT_LO
        b"\xaa\x03\x05ab",                          # 截断于 READ_PAYLOAD
        data3[:-1],                                 # 截断于 READ_CHECKSUM
        ping + data3 + ext0,                        # 背靠背帧
        build_frame(0x03, b"\xaa\xaa"),             # 负载内含 0xAA
        b"\xaa\x03\x02a",                           # 截断于 PAYLOAD 中间（剩 1 字节）
    ]
    return streams


def random_stream(rng):
    """随机拼接合法帧 / 截断帧 / 垃圾字节 / 错误帧。"""
    parts = []
    for _ in range(rng.randrange(1, 8)):
        kind = rng.randrange(9)
        if kind == 0:
            parts.append(bytes(rng.randrange(256) for _ in range(rng.randrange(1, 12))))
        elif kind == 1:
            payload = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 20)))
            parts.append(build_frame(rng.choice(list(TYPES)), payload))
        elif kind == 2:
            payload = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 300)))
            parts.append(build_frame(rng.choice(list(TYPES)), payload, ext=True))
        elif kind == 3:
            parts.append(build_frame(rng.choice(list(TYPES)), b"payload",
                                     checksum_delta=rng.randrange(1, 256)))
        elif kind == 4:
            bad = rng.choice([t for t in range(256) if t not in TYPES])
            parts.append(bytes([MAGIC, bad]) + bytes(rng.randrange(256)
                                                     for _ in range(rng.randrange(0, 5))))
        elif kind == 5:
            parts.append(build_frame(rng.choice(list(TYPES)), b""))
        elif kind == 6:
            frame = build_frame(rng.choice(list(TYPES)), b"abc")
            parts.append(frame[:rng.randrange(1, len(frame))])
        elif kind == 7:
            parts.append(bytes([MAGIC, rng.choice(list(TYPES)), 0xFF,
                                rng.randrange(256), rng.randrange(256)]))
        else:
            parts.append(build_frame(rng.choice(list(TYPES)), b"", ext=True))
    return b"".join(parts)


def all_chunkings(data):
    """枚举 data 的全部 2^(n-1) 种切分（仅用于短输入）。"""
    n = len(data)
    if n == 0:
        yield [b""]
        return
    for mask in range(1 << (n - 1)):
        chunks, last = [], 0
        for i in range(n - 1):
            if mask & (1 << i):
                chunks.append(data[last:i + 1])
                last = i + 1
        chunks.append(data[last:])
        yield chunks


def random_chunkings(rng, data, count):
    """随机切分 count 次（含整块与逐字节两种极端）。"""
    yield [data]
    if data:
        yield [data[i:i + 1] for i in range(len(data))]
    for _ in range(count):
        chunks, i = [], 0
        while i < len(data):
            step = rng.randrange(1, len(data) - i + 1)
            chunks.append(data[i:i + step])
            i += step
        yield chunks or [b""]
