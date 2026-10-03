"""端到端演示：在内存管道上协商并收发消息，展示线路上的真实帧。

运行：python3 examples/demo.py
"""

import io
import threading

from protoext import Session
from protoext.legacy import LegacySession
from protoext.wire import read_frame
from tests._harness import pipe_pair


def spy_frames(writer):
    """截获写出的原始帧，打印“线路上到底有什么”。"""
    original_write = writer.write
    frames = []

    def spy(data):
        frames.append(read_frame(io.BytesIO(data)))
        return original_write(data)

    writer.write = spy
    return frames


def scenario(title, client_factory, server_factory):
    print("-" * 72)
    print(title)
    print("-" * 72)
    (cio, sio) = pipe_pair()
    client = client_factory(cio)
    server = server_factory(sio)
    cframes = spy_frames(client.writer)
    sframes = spy_frames(server.writer)

    server_err = []

    def run_server():
        try:
            server.handshake()
            agreed = getattr(server, "agreed", [])
            print("服务端协商结果:", agreed,
                  "| legacy:", getattr(server, "legacy", True))
            msg = server.recv_data()
            print("服务端收到:", msg)
            if getattr(server, "has_extension", None) and \
                    server.has_extension("priority"):
                server.send_data(f"ack:{msg['payload']}", priority=1)
            else:
                server.send_data(f"ack:{msg['payload']}")
            server.send_fin()
        except Exception as exc:  # noqa: BLE001
            server_err.append(exc)

    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    client.handshake()
    print("客户端协商结果:", client.agreed, "| legacy:", client.legacy)

    opts = {"priority": 3, "compress": True} \
        if client.has_extension("zlib") else {}
    client.send_data("hello extensions", **opts)
    reply = client.recv_data()
    print("客户端收到:", reply)
    t.join(5)
    if server_err:
        raise server_err[0]

    print("\n线路上客户端发出的帧（未协商扩展的字段完全不存在）:")
    for f in cframes:
        print("  ", f)
    print("线路上服务端发出的帧:")
    for f in sframes:
        print("  ", f)
    print()


def main():
    scenario(
        "场景 A：新版 <-> 新版（双方支持全部扩展）",
        lambda pair: Session(pair[0], pair[1], "client"),
        lambda pair: Session(pair[0], pair[1], "server"),
    )
    scenario(
        "场景 B：新版 <-> 新版（服务端只支持 priority）",
        lambda pair: Session(pair[0], pair[1], "client"),
        lambda pair: Session(pair[0], pair[1], "server",
                             offered=["priority"]),
    )
    scenario(
        "场景 C：新版 <-> 旧版（对端无协商字段，自动按基础协议运行）",
        lambda pair: Session(pair[0], pair[1], "client"),
        lambda pair: LegacySession(pair[0], pair[1], "server"),
    )


if __name__ == "__main__":
    main()
