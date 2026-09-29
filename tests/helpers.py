"""Shared corpus builders for the parser test-suite."""

import random
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from legacy_parser import MAGIC1, MAGIC2  # noqa: E402

TYPE_IDS = {"DATA": 0x01, "ACK": 0x02, "PING": 0x03, "PONG": 0x04}


def make_frame(type_byte, payload):
    """Build a well-formed frame."""
    body = bytes([type_byte, (len(payload) >> 8) & 0xFF, len(payload) & 0xFF])
    body += bytes(payload)
    crc = 0
    for b in body:
        crc ^= b
    return bytes([MAGIC1, MAGIC2]) + body + bytes([crc])


def hand_crafted_streams():
    """Targeted streams covering every error path and edge case."""
    streams = {
        "empty": b"",
        "single_ping": make_frame(0x03, b""),
        "all_types": b"".join([
            make_frame(0x01, b"hello"),
            make_frame(0x02, b"\x00\x07"),
            make_frame(0x03, b""),
            make_frame(0x04, b""),
        ]),
        "noise_then_frame": b"\x00\x11\x22" + make_frame(0x01, b"x"),
        "bad_magic2": bytes([MAGIC1, 0x00]) + make_frame(0x03, b""),
        "double_magic1": bytes([MAGIC1, MAGIC1, MAGIC2, 0x03, 0, 0, 0x03]),
        "magic1_as_error_byte": bytes([MAGIC1, MAGIC1]) + make_frame(0x04, b""),
        "unknown_type": bytes([MAGIC1, MAGIC2, 0x7F, 0, 0, 0x7F]),
        "unknown_type_then_frame": bytes([MAGIC1, MAGIC2, 0x7F]) + make_frame(0x03, b""),
        "ping_with_payload": bytes([MAGIC1, MAGIC2, 0x03, 0, 3, 1, 2, 3, 0]),
        "pong_with_payload": bytes([MAGIC1, MAGIC2, 0x04, 0, 1, 9, 0]),
        "ack_wrong_length": bytes([MAGIC1, MAGIC2, 0x02, 0, 5, 1, 2, 3, 4, 5, 0]),
        "len_too_big": bytes([MAGIC1, MAGIC2, 0x01, 0x04, 0x01]) + b"\x00" * 8,
        "type_rule_beats_max": bytes([MAGIC1, MAGIC2, 0x03, 0x05, 0x00]),
        "bad_crc": make_frame(0x01, b"abc")[:-1] + b"\xFF",
        "bad_crc_then_frame": (make_frame(0x01, b"abc")[:-1] + b"\xFF"
                               + make_frame(0x02, b"\x12\x34")),
        "truncated_magic": bytes([MAGIC1]),
        "truncated_header": bytes([MAGIC1, MAGIC2, 0x01]),
        "truncated_len": bytes([MAGIC1, MAGIC2, 0x01, 0x00]),
        "truncated_payload": bytes([MAGIC1, MAGIC2, 0x01, 0x00, 0x05, 1, 2]),
        "truncated_crc": make_frame(0x01, b"abc")[:-1],
        "max_payload": make_frame(0x01, bytes(1024)),
        "magic_inside_payload": make_frame(0x01, bytes([MAGIC1, MAGIC2, MAGIC1])),
        "frame_then_garbage": make_frame(0x03, b"") + b"\x99\x88" + make_frame(0x04, b""),
        "interleaved_junk": b"\xAB\xAB\x00" + make_frame(0x01, b"\xAB") + b"\xAB\xCD",
    }
    return streams


def random_streams(count, seed):
    """Seeded random streams: garbage mixed with (sometimes broken) frames."""
    rng = random.Random(seed)
    streams = []
    for _ in range(count):
        parts = []
        for _ in range(rng.randrange(0, 6)):
            roll = rng.random()
            if roll < 0.45:
                parts.append(bytes(rng.randrange(256)
                                   for _ in range(rng.randrange(0, 24))))
            else:
                t = rng.choice(list(TYPE_IDS.values()))
                n = rng.choice([0, 1, 2, 3, 5, 16, 1024, 1025])
                payload = bytes(rng.randrange(256) for _ in range(n))
                frame = make_frame(t, payload)
                if rng.random() < 0.35 and frame:
                    # corrupt a random byte / truncate
                    if rng.random() < 0.5:
                        i = rng.randrange(len(frame))
                        frame = frame[:i] + bytes([rng.randrange(256)]) + frame[i + 1:]
                    else:
                        frame = frame[:rng.randrange(len(frame) + 1)]
                parts.append(frame)
        streams.append(b"".join(parts))
    return streams
