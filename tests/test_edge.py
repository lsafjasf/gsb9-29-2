"""边界用例：畸形帧、超大帧、错误消息类型、版本不匹配、对端中途断开。"""

import socket
import struct
import threading
import unittest

from protoext.codec import SocketTransport, send_message
from protoext.errors import (
    FrameTooLargeError,
    MalformedMessageError,
    ProtocolError,
    UnexpectedMessageError,
)
from protoext.session import Session

from util import new_client, new_server, run_pair


def drive_raw(raw_client_body, server_extensions=()):
    """raw_client_body(sock) 完全手工控制客户端字节流；server 用正式实现。"""
    csock, ssock = socket.socketpair()
    csock.settimeout(5)
    ssock.settimeout(5)
    box = {}

    def server_thread():
        try:
            Session(SocketTransport(ssock), "server",
                    extensions=server_extensions).handshake()
        except Exception as exc:  # noqa: BLE001
            box["server_err"] = exc

    t = threading.Thread(target=server_thread)
    t.start()
    try:
        raw_client_body(csock)
    finally:
        csock.close()
    t.join(8)
    assert not t.is_alive(), "server 线程超时未退出"
    ssock.close()
    return box


class EdgeCaseTests(unittest.TestCase):

    def test_malformed_json_frame(self):
        def raw(sock):
            sock.sendall(struct.pack(">I", 5) + b"{oops")
        box = drive_raw(raw)
        self.assertIsInstance(box.get("server_err"), MalformedMessageError)

    def test_non_object_frame(self):
        def raw(sock):
            payload = b"[1,2,3]"
            sock.sendall(struct.pack(">I", len(payload)) + payload)
        box = drive_raw(raw)
        self.assertIsInstance(box.get("server_err"), MalformedMessageError)

    def test_frame_too_large(self):
        def raw(sock):
            sock.sendall(struct.pack(">I", 2 << 20))  # 声明 2MiB，超过 1MiB 上限
        box = drive_raw(raw)
        self.assertIsInstance(box.get("server_err"), FrameTooLargeError)

    def test_wrong_message_type_during_handshake(self):
        def raw(sock):
            send_message(SocketTransport(sock),
                         {"type": "data", "seq": 0, "payload": "too early"})
        box = drive_raw(raw)
        self.assertIsInstance(box.get("server_err"), UnexpectedMessageError)

    def test_protocol_version_mismatch(self):
        def raw(sock):
            send_message(SocketTransport(sock),
                         {"type": "hello", "proto": 999, "nonce": "00"})
        box = drive_raw(raw)
        self.assertIsInstance(box.get("server_err"), ProtocolError)

    def test_peer_disconnects_mid_handshake(self):
        def raw(sock):
            pass  # 直接关闭，一个字节都不发
        box = drive_raw(raw)
        self.assertIsInstance(box.get("server_err"), (EOFError, OSError))

    def test_ext_field_wrong_type(self):
        def raw(sock):
            send_message(SocketTransport(sock),
                         {"type": "hello", "proto": 1, "nonce": "00",
                          "ext": "compression"})  # 应为数组
        box = drive_raw(raw, server_extensions=["compression"])
        from protoext.errors import ContradictionError
        self.assertIsInstance(box.get("server_err"), ContradictionError)

    def test_empty_payload_data_roundtrip(self):
        result = run_pair(
            new_client([]), new_server([]),
            lambda s: (s.send_data(""), s.recv_data())[1],
            lambda s: s.send_data(s.recv_data()["payload"]),
        )
        self.assertIsNone(result["client"].error)
        self.assertEqual(result["client"].output["payload"], "")

    def test_many_sequential_messages(self):
        # 一问一答 200 轮（本环境 socketpair 缓冲仅约 4KB，批量单发会死锁）
        def client_flow(s):
            replies = []
            for i in range(200):
                s.send_data(f"m{i}", ext={"priority": {"prio": i % 10}})
                replies.append(s.recv_data()["payload"])
            return replies

        def server_flow(s):
            for _ in range(200):
                msg = s.recv_data()
                s.send_data("r:" + msg["payload"],
                            ext={"priority": {"prio": msg["prio"]}})

        result = run_pair(new_client(["priority"]), new_server(["priority"]),
                          client_flow, server_flow)
        self.assertIsNone(result["client"].error)
        self.assertIsNone(result["server"].error)
        self.assertEqual(result["client"].output[0], "r:m0")
        self.assertEqual(result["client"].output[199], "r:m199")


if __name__ == "__main__":
    unittest.main()
