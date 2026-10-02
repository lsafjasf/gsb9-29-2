#!/usr/bin/env python3
"""演示：扩展协商结果、互操作矩阵、错误样例。

运行：python3 examples/demo.py
"""

import socket
import sys
import threading
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protoext.codec import SocketTransport
from protoext.legacy import LegacySession
from protoext.session import Session

TIMEOUT = 5


def run(make_client, make_server, client_fn=None, server_fn=None):
    csock, ssock = socket.socketpair()
    box = {}

    def drive(side, make, sock, fn):
        try:
            session = make(SocketTransport(sock, timeout=TIMEOUT))
            if hasattr(session, "handshake"):
                session.handshake()
            if fn:
                fn(session)
            box[side] = session
        except Exception as exc:  # noqa: BLE001
            box[side + "_err"] = exc

    threads = [
        threading.Thread(target=drive, args=("client", make_client, csock, client_fn)),
        threading.Thread(target=drive, args=("server", make_server, ssock, server_fn)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return box


def new(role, exts, required=()):
    return lambda t: Session(t, role, extensions=exts, required=required)


def old(role):
    return lambda t: LegacySession(t, role)


def section(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(name, box):
    print(f"[{name}]")
    for side in ("client", "server"):
        session = box.get(side)
        if session is not None:
            negotiated = getattr(session, "negotiated", None)
            legacy = getattr(session, "peer_legacy", None)
            desc = "旧版(无协商能力)" if negotiated is None else (
                f"negotiated={sorted(negotiated)} legacy_peer={legacy}")
            print(f"  {side}: {desc}")
        if side + "_err" in box:
            print(f"  {side} ERROR: {box[side + '_err']}")


def main():
    section("1) 互操作矩阵")
    show("新{c,p} x 新{c,p}        全部支持",
         run(new("client", ["compression", "priority"]),
             new("server", ["compression", "priority"])))
    show("新{c,p} x 新{p}          部分支持",
         run(new("client", ["compression", "priority"]),
             new("server", ["priority"])))
    show("新{c}   x 新{p}          完全不支持",
         run(new("client", ["compression"]), new("server", ["priority"])))
    show("新{c,p} x 旧版           老服务端",
         run(new("client", ["compression", "priority"]), old("server")))
    show("旧版   x 新{c,p}         老客户端",
         run(old("client"), new("server", ["compression", "priority"])))
    show("旧版   x 旧版            双老",
         run(old("client"), old("server")))

    section("2) 错误样例：必需扩展缺失（双方能力清单随错误带出）")
    box = run(new("client", ["compression", "priority"], required=["compression"]),
              new("server", ["priority"]))
    show("required=compression, 服务端只有 priority", box)

    section("3) 错误样例：对端声明矛盾")
    import hashlib
    from protoext.codec import recv_message, send_message
    from protoext.session import _HELLO_HASH_CONTEXT

    def rogue_server(t):
        hello, raw = recv_message(t)
        send_message(t, {
            "type": "hello_ack", "proto": 1, "nonce": "00",
            "ext": ["compression", "made-up-ext"],
            "hello_hash": hashlib.sha256(_HELLO_HASH_CONTEXT + raw).hexdigest(),
        })
        try:
            recv_message(t)
        except Exception:
            pass

    box = run(new("client", ["compression", "priority"]), rogue_server)
    show("服务端 ack 了客户端从未提供的 made-up-ext", box)

    section("4) 错误样例：协商字段被篡改")
    from tests.util import run_mitm_pair
    def strip_ext(msg):
        if msg.get("type") == "hello":
            msg = dict(msg)
            msg.pop("ext", None)
        return msg
    result = run_mitm_pair(new("client", ["compression", "priority"]),
                           new("server", ["compression", "priority"]),
                           c2s_transform=strip_ext)
    print("[MITM 剥离 hello 中的 ext]")
    for side in ("client", "server"):
        r = result[side]
        if r.error is not None:
            print(f"  {side} ERROR: {type(r.error).__name__}: {r.error}")
        else:
            print(f"  {side} ok: negotiated={sorted(r.session.negotiated)}")

    section("5) 数据面可见性：未协商扩展字段收发双向拒绝")
    box = run(new("client", ["compression", "priority"]),
              new("server", ["priority"]))
    client = box["client"]
    try:
        client.send_data("x", ext={"compression": {"c": "gzip"}})
    except Exception as exc:
        print(f"发送未协商扩展字段 -> {type(exc).__name__}: {exc}")
    client.send_data("ok", ext={"priority": {"prio": 7}})
    print(f"已协商扩展字段正常收发: prio={box['server'].recv_data()['prio']}")
    send_message(box["server"].transport,
                 {"type": "data", "seq": 0, "payload": "x", "c": "gzip"})
    try:
        client.recv_data()
    except Exception as exc:
        print(f"接收未协商扩展字段 -> {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
