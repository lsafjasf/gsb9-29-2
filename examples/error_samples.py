"""打印各类协商错误的真实样例输出（含双方能力清单）。

运行：python3 examples/error_samples.py
"""

import threading

from protoext import (
    Session, NegotiationFailed, NegotiationContradiction,
    NegotiationTampered,
)
from tests._harness import mitm_pair, pipe_pair, run_handshake


def banner(title):
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72)


def sample_required_missing():
    banner("1) 协商失败：客户端 required=zlib，服务端只支持 priority")
    (cio, sio) = pipe_pair()
    client = Session(cio[0], cio[1], "client",
                     offered=["priority", "zlib"], required=["zlib"])
    server = Session(sio[0], sio[1], "server", offered=["priority"])
    _, t, server_errors = run_handshake(server)
    try:
        client.handshake()
    except NegotiationFailed as err:
        print("[客户端抛出]")
        print(type(err).__name__, ":", str(err))
    t.join(5)
    print("\n[服务端抛出]")
    print(type(server_errors[0]).__name__, ":", str(server_errors[0]))


def sample_ack_contradiction():
    banner("2) 声明矛盾：ack 包含客户端从未提供的扩展")
    client_io, server_io = mitm_pair(
        lambda d, f: dict(f, ext=["priority", "zlib", "telepathy"])
        if d == "s2c" and f.get("type") == "hello_ack" else None)
    client = Session(client_io[0], client_io[1], "client",
                     offered=["priority", "zlib"])
    server = Session(server_io[0], server_io[1], "server")
    _, t, _ = run_handshake(server)
    try:
        client.handshake()
    except NegotiationContradiction as err:
        print(type(err).__name__, ":", str(err))
    t.join(5)


def sample_use_unnegotiated():
    banner("3) 声明矛盾：数据帧使用未协商成功的扩展字段")
    import json
    import struct
    (cio, sio) = pipe_pair()
    client = Session(cio[0], cio[1], "client", offered=["zlib"])
    server = Session(sio[0], sio[1], "server", offered=["zlib"])
    _, t, _ = run_handshake(server)
    client.handshake()
    t.join(5)
    rogue = json.dumps({"type": "data", "seq": 0,
                        "payload": "x", "pri": 0}).encode()
    server.writer.write(struct.pack(">I", len(rogue)) + rogue)
    try:
        client.recv_data()
    except NegotiationContradiction as err:
        print(type(err).__name__, ":", str(err))


def sample_tampered():
    banner("4) 协商字段被篡改：MITM 从 hello 中删除 zlib")

    def rule(direction, frame):
        if direction == "c2s" and frame.get("type") == "hello":
            frame = dict(frame)
            frame["ext"] = ["priority"]
        return frame

    client_io, server_io = mitm_pair(rule)
    client = Session(client_io[0], client_io[1], "client")
    server = Session(server_io[0], server_io[1], "server")
    _, t, server_errors = run_handshake(server)
    try:
        client.handshake()
    except NegotiationTampered as err:
        print("[客户端抛出]")
        print(type(err).__name__, ":", str(err))
    t.join(5)
    print("\n[服务端抛出]")
    print(type(server_errors[0]).__name__, ":", str(server_errors[0]))


def sample_legacy_required():
    banner("5) 必需扩展遇到旧对端（禁止降级）")
    (cio, sio) = pipe_pair()
    from protoext.legacy import LegacySession
    client = Session(cio[0], cio[1], "client", required=["zlib"],
                     allow_legacy=False)
    legacy = LegacySession(sio[0], sio[1], "server")
    _, t, _ = run_handshake(legacy)
    try:
        client.handshake()
    except NegotiationFailed as err:
        print(type(err).__name__, ":", str(err))
    t.join(5)


if __name__ == "__main__":
    sample_required_missing()
    sample_ack_contradiction()
    sample_use_unnegotiated()
    sample_tampered()
    sample_legacy_required()
