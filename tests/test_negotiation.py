"""协商语义自测：全部支持 / 部分支持 / 完全不支持 / 矛盾声明 / 必需扩展。"""

import hashlib
import socket
import threading
import unittest

from protoext.codec import SocketTransport, recv_message, send_message
from protoext.errors import (
    ContradictionError,
    ExtensionNotNegotiatedError,
    NegotiationFailedError,
    ProtocolError,
    UnknownFieldError,
    UnnegotiatedExtensionError,
)
from protoext.session import _HELLO_HASH_CONTEXT, Session

from util import new_client, new_server, run_pair

ALL = ["compression", "priority"]


def drive_rogue_pair(client_body, server_body):
    """手工驱动两端原始协程，便于注入非法消息。返回双方异常盒子。"""
    csock, ssock = socket.socketpair()
    csock.settimeout(5)
    ssock.settimeout(5)
    box = {}
    threads = [
        threading.Thread(target=client_body, args=(SocketTransport(csock), box)),
        threading.Thread(target=server_body, args=(SocketTransport(ssock), box)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(8)
        assert not t.is_alive(), "协程超时未退出"
    csock.close()
    ssock.close()
    return box


class NegotiationTests(unittest.TestCase):

    # --------------------------------------------------------- 全部支持

    def test_full_support(self):
        def client_data(session):
            session.send_data("hi", ext={"priority": {"prio": 5},
                                         "compression": {"c": "gzip"}})
            reply = session.recv_data()
            self.assertEqual(reply["prio"], 5)
            self.assertEqual(reply["c"], "gzip")

        def server_data(session):
            msg = session.recv_data()
            self.assertEqual(msg["payload"], "hi")
            session.send_data("ack", ext={"priority": {"prio": msg["prio"]},
                                          "compression": {"c": msg["c"]}})

        result = run_pair(new_client(ALL), new_server(ALL), client_data, server_data)
        self.assertIsNone(result["client"].error)
        self.assertIsNone(result["server"].error)
        self.assertEqual(result["client"].session.negotiated, set(ALL))
        self.assertEqual(result["server"].session.negotiated, set(ALL))

    # --------------------------------------------------------- 部分支持

    def test_partial_support(self):
        def client_flow(session):
            # 发送侧：未协商的 compression 字段不可发送
            with self.assertRaises(ExtensionNotNegotiatedError):
                session.send_data("x", ext={"compression": {"c": "gzip"}})
            # 已协商的 priority 可以发送
            session.send_data("x", ext={"priority": {"prio": 1}})
            return session.recv_data()

        def server_flow(session):
            msg = session.recv_data()
            self.assertEqual(msg["prio"], 1)
            session.send_data("ack")

        result = run_pair(new_client(ALL), new_server(["priority"]),
                          client_flow, server_flow)
        self.assertIsNone(result["client"].error)
        self.assertIsNone(result["server"].error)
        self.assertEqual(result["client"].session.negotiated, {"priority"})
        self.assertEqual(result["server"].session.negotiated, {"priority"})
        self.assertEqual(result["client"].output["payload"], "ack")

    # --------------------------------------------------------- 完全不支持

    def test_no_common_extensions(self):
        def client_flow(session):
            session.send_data("base-only")
            with self.assertRaises(ExtensionNotNegotiatedError):
                session.send_data("x", ext={"compression": {"c": "gzip"}})
            return session.recv_data()

        def server_flow(session):
            msg = session.recv_data()
            self.assertEqual(msg["payload"], "base-only")
            with self.assertRaises(ExtensionNotNegotiatedError):
                session.send_data("x", ext={"priority": {"prio": 3}})
            session.send_data("ack")

        result = run_pair(new_client(["compression"]), new_server(["priority"]),
                          client_flow, server_flow)
        self.assertIsNone(result["client"].error)
        self.assertIsNone(result["server"].error)
        self.assertEqual(result["client"].session.negotiated, frozenset())
        self.assertEqual(result["server"].session.negotiated, frozenset())
        self.assertEqual(result["client"].output["payload"], "ack")

    def test_both_new_with_empty_extensions(self):
        # 两端都是新版本但都不带扩展：仍完成完整握手（含 finished 校验）
        result = run_pair(new_client([]), new_server([]))
        self.assertIsNone(result["client"].error)
        self.assertIsNone(result["server"].error)
        self.assertFalse(result["client"].session.peer_legacy)
        self.assertFalse(result["server"].session.peer_legacy)

    # --------------------------------------------------------- 必需扩展

    def test_required_satisfied(self):
        result = run_pair(
            new_client(["compression", "priority"], required=["compression"]),
            new_server(["compression"]),
        )
        self.assertIsNone(result["client"].error)
        self.assertEqual(result["client"].session.negotiated, {"compression"})

    def test_required_missing_fails_with_capability_lists(self):
        result = run_pair(
            new_client(["compression", "priority"], required=["compression"]),
            new_server(["priority"]),
        )
        client_err = result["client"].error
        server_err = result["server"].error
        self.assertIsInstance(client_err, NegotiationFailedError)
        self.assertIsInstance(server_err, NegotiationFailedError)
        # 双方能力清单随错误带出
        self.assertEqual(client_err.local_caps, tuple(sorted(ALL)))
        self.assertEqual(client_err.remote_caps, ("priority",))
        self.assertEqual(client_err.missing, ("compression",))
        self.assertEqual(server_err.missing, ("compression",))
        self.assertEqual(server_err.local_caps, ("priority",))
        self.assertEqual(server_err.remote_caps, tuple(sorted(ALL)))

    # --------------------------------------------------------- 矛盾声明

    def test_server_accepts_extension_never_offered(self):
        def client_body(t, box):
            session = Session(t, "client", extensions=ALL)
            try:
                session.handshake()
            except Exception as exc:  # noqa: BLE001
                box["client_err"] = exc

        def server_body(t, box):
            hello, raw = recv_message(t)
            send_message(t, {
                "type": "hello_ack",
                "proto": 1,
                "nonce": "00",
                "ext": ["compression", "completely-made-up"],
                "hello_hash": hashlib.sha256(_HELLO_HASH_CONTEXT + raw).hexdigest(),
            })
            try:
                recv_message(t)
            except Exception:  # noqa: BLE001
                pass

        box = drive_rogue_pair(client_body, server_body)
        self.assertIsInstance(box.get("client_err"), ContradictionError)

    def test_hello_requires_extension_not_offered(self):
        def client_body(t, box):
            send_message(t, {
                "type": "hello", "proto": 1, "nonce": "00",
                "ext": ["priority"],
                "ext_required": ["compression"],
            })
            try:
                recv_message(t)
            except Exception:  # noqa: BLE001
                pass

        def server_body(t, box):
            session = Session(t, "server", extensions=ALL)
            try:
                session.handshake()
            except Exception as exc:  # noqa: BLE001
                box["server_err"] = exc

        box = drive_rogue_pair(client_body, server_body)
        self.assertIsInstance(box.get("server_err"), ContradictionError)

    # --------------------------------------------------------- 注册表边界

    def test_unknown_local_extension_rejected_at_construction(self):
        a, _ = socket.socketpair()
        with self.assertRaises(ValueError):
            Session(SocketTransport(a), "client", extensions=["does-not-exist"])
        a.close()

    def test_required_must_be_subset_of_extensions(self):
        a, _ = socket.socketpair()
        with self.assertRaises(ValueError):
            Session(SocketTransport(a), "client",
                    extensions=["priority"], required=["compression"])
        a.close()

    def test_duplicate_extension_entries_normalized(self):
        def client_body(t, box):
            send_message(t, {"type": "hello", "proto": 1, "nonce": "00",
                             "ext": ["priority", "priority", "compression"]})
            ack, _ = recv_message(t)
            box["ack"] = ack
            # 补一个 finished 让 server 走完流程（内容无所谓，server 校验失败会回 error）
            send_message(t, {"type": "finished", "verify": "0" * 64})
            try:
                recv_message(t)
            except Exception:  # noqa: BLE001
                pass

        def server_body(t, box):
            session = Session(t, "server", extensions=ALL)
            try:
                session.handshake()
                box["negotiated"] = session.negotiated
            except Exception as exc:  # noqa: BLE001
                box["server_err"] = exc

        box = drive_rogue_pair(client_body, server_body)
        self.assertEqual(box["ack"]["ext"].count("priority"), 1)
        self.assertEqual(sorted(box["ack"]["ext"]), ALL)

    def test_unknown_extension_offered_by_peer_is_skipped(self):
        # 对端提供了注册表之外的扩展名：不接受即可，不算错误
        def client_body(t, box):
            send_message(t, {"type": "hello", "proto": 1, "nonce": "00",
                             "ext": ["priority", "future-ext"]})
            ack, _ = recv_message(t)
            box["ack"] = ack

        def server_body(t, box):
            session = Session(t, "server", extensions=["priority"])
            try:
                session.handshake()
            except Exception:  # noqa: BLE001 - client 不发 finished，server 报错属预期
                pass

        box = drive_rogue_pair(client_body, server_body)
        self.assertEqual(box["ack"]["ext"], ["priority"])

    # --------------------------------------------------------- 数据面可见性

    def test_unnegotiated_extension_field_rejected_on_receive(self):
        def client_flow(session):
            with self.assertRaises(UnnegotiatedExtensionError):
                session.recv_data()

        def server_flow(session):
            # 伪造一个携带未协商 compression 字段的数据帧
            send_message(session.transport, {"type": "data", "seq": 0,
                                             "payload": "evil", "c": "gzip"})

        result = run_pair(new_client(ALL), new_server(["priority"]),
                          client_flow, server_flow)
        self.assertIsNone(result["client"].error)
        self.assertIsNone(result["server"].error)

    def test_unknown_field_rejected_on_receive(self):
        def client_flow(session):
            with self.assertRaises(UnknownFieldError):
                session.recv_data()

        def server_flow(session):
            send_message(session.transport,
                         {"type": "data", "seq": 0, "payload": "evil", "zzz": 1})

        result = run_pair(new_client([]), new_server([]),
                          client_flow, server_flow)
        self.assertIsNone(result["client"].error)
        self.assertIsNone(result["server"].error)

    def test_send_before_handshake_rejected(self):
        a, _ = socket.socketpair()
        session = Session(SocketTransport(a), "client", extensions=ALL)
        with self.assertRaises(ProtocolError):
            session.send_data("x")
        with self.assertRaises(ProtocolError):
            session.recv_data()
        a.close()

    def test_double_handshake_rejected(self):
        result = run_pair(new_client([]), new_server([]))
        with self.assertRaises(ProtocolError):
            result["client"].session.handshake()


if __name__ == "__main__":
    unittest.main()
