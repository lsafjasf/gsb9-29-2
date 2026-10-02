"""新旧版本互操作矩阵。

矩阵（C=客户端, S=服务端）：

    #   客户端            服务端            期望结果
    1   新 {c,p}          新 {c,p}          协商 {c,p}，扩展字段双向可见
    2   新 {c,p}          新 {p}            协商 {p}，compression 完全不可见
    3   新 {c}            新 {p}            协商 {}，基础协议
    4   新 {c,p}          旧版              协商 {}，线上无任何扩展字段
    5   旧版              新 {c,p}          协商 {}，线上无任何扩展字段
    6   旧版              旧版              基础协议
    7   旧版 + 新字段     旧版              老对端解析失败（问题起源，反例）

c = compression, p = priority
"""

import unittest

from protoext.errors import UnknownFieldError
from protoext.legacy import LegacySession

from util import (
    RecordingTransport,
    legacy_client,
    legacy_server,
    new_client,
    new_server,
    run_pair,
)

ALL = ["compression", "priority"]
BASE_KEYS = {"type", "seq", "payload"}


class InteropMatrixTests(unittest.TestCase):

    # 1. 新版 <-> 新版，全部支持
    def test_1_new_new_full(self):
        result = run_pair(new_client(ALL), new_server(ALL))
        self.assertIsNone(result["client"].error)
        self.assertEqual(result["client"].session.negotiated, set(ALL))
        self.assertEqual(result["server"].session.negotiated, set(ALL))

    # 2. 新版 <-> 新版，部分支持
    def test_2_new_new_partial(self):
        result = run_pair(new_client(ALL), new_server(["priority"]))
        self.assertIsNone(result["client"].error)
        self.assertEqual(result["client"].session.negotiated, {"priority"})
        self.assertEqual(result["server"].session.negotiated, {"priority"})

    # 3. 新版 <-> 新版，无交集
    def test_3_new_new_no_overlap(self):
        result = run_pair(new_client(["compression"]), new_server(["priority"]))
        self.assertIsNone(result["client"].error)
        self.assertEqual(result["client"].session.negotiated, frozenset())
        self.assertEqual(result["server"].session.negotiated, frozenset())

    # 4. 新版客户端 <-> 旧版服务端
    def test_4_new_client_legacy_server(self):
        recorded = {}

        def make_client(transport):
            rec = RecordingTransport(transport)
            recorded["rec"] = rec
            from protoext.session import Session
            return Session(rec, "client", extensions=ALL)

        def client_flow(session):
            session.send_data("hello legacy")
            return session.recv_data()

        def server_flow(session):
            msg = session.recv_data()
            session.send_data("reply:" + msg["payload"])

        result = run_pair(make_client, legacy_server(), client_flow, server_flow)
        self.assertIsNone(result["client"].error)
        self.assertIsNone(result["server"].error)
        self.assertEqual(result["client"].output["payload"], "reply:hello legacy")
        self.assertEqual(result["client"].session.negotiated, frozenset())
        self.assertTrue(result["client"].session.peer_legacy)

        # 线上可见性：客户端发出的 data 帧只含基础字段
        data_frames = [f for f in recorded["rec"].sent_frames()
                       if f.get("type") == "data"]
        self.assertTrue(data_frames)
        for frame in data_frames:
            self.assertLessEqual(set(frame), BASE_KEYS)

    # 5. 旧版客户端 <-> 新版服务端
    def test_5_legacy_client_new_server(self):
        recorded = {}

        def make_server(transport):
            rec = RecordingTransport(transport)
            recorded["rec"] = rec
            from protoext.session import Session
            return Session(rec, "server", extensions=ALL)

        def client_flow(session):
            session.send_data("ping")
            return session.recv_data()

        def server_flow(session):
            msg = session.recv_data()
            session.send_data("pong:" + msg["payload"])

        result = run_pair(legacy_client(), make_server, client_flow, server_flow)
        self.assertIsNone(result["client"].error)
        self.assertIsNone(result["server"].error)
        self.assertEqual(result["client"].output["payload"], "pong:ping")
        self.assertEqual(result["server"].session.negotiated, frozenset())
        self.assertTrue(result["server"].session.peer_legacy)

        # 服务端发出的 hello_ack 不带 ext，data 帧只含基础字段
        frames = recorded["rec"].sent_frames()
        ack = next(f for f in frames if f.get("type") == "hello_ack")
        self.assertNotIn("ext", ack)
        for frame in frames:
            if frame.get("type") == "data":
                self.assertLessEqual(set(frame), BASE_KEYS)

    # 6. 旧版 <-> 旧版
    def test_6_legacy_legacy(self):
        result = run_pair(
            legacy_client(), legacy_server(),
            lambda s: (s.send_data("a"), s.recv_data())[1],
            lambda s: s.send_data("b:" + s.recv_data()["payload"]),
        )
        self.assertIsNone(result["client"].error)
        self.assertIsNone(result["server"].error)
        self.assertEqual(result["client"].output["payload"], "b:a")

    # 7. 反例：老对端收到未协商的新字段会解析失败（本库要解决的问题）
    def test_7_legacy_peer_breaks_on_new_fields(self):
        import socket
        from protoext.codec import SocketTransport, send_message

        csock, ssock = socket.socketpair()
        csock.settimeout(5)
        ssock.settimeout(5)
        legacy = LegacySession(SocketTransport(csock), "client")

        import threading
        box = {}

        def client_thread():
            try:
                legacy.handshake()
                legacy.recv_data()
            except Exception as exc:  # noqa: BLE001
                box["err"] = exc

        def server_thread():
            t = SocketTransport(ssock)
            from protoext.codec import recv_message
            recv_message(t)  # hello
            send_message(t, {"type": "hello_ack", "proto": 1, "nonce": "00"})
            # 直接发带新字段的 data（未协商）—— 老客户端解析失败
            send_message(t, {"type": "data", "seq": 0, "payload": "x", "c": "gzip"})

        tc = threading.Thread(target=client_thread)
        ts = threading.Thread(target=server_thread)
        tc.start(); ts.start()
        tc.join(8); ts.join(8)
        self.assertIsInstance(box.get("err"), UnknownFieldError)


if __name__ == "__main__":
    unittest.main()
