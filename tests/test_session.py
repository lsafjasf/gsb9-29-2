import io
import unittest

from protoext import (
    Session, ExtensionNotNegotiated, NegotiationFailed,
    NegotiationContradiction, NegotiationTampered, ConnectionClosed,
    ProtocolError,
)
from protoext.legacy import LegacySession
from tests._harness import make_sessions, connect, run_handshake, SpyWriter


class FullSupportTests(unittest.TestCase):
    def test_all_extensions_negotiated_and_visible(self):
        client, server = make_sessions()
        connect(client, server)
        self.assertFalse(client.legacy)
        self.assertEqual(client.agreed, ["priority", "zlib"])
        self.assertEqual(server.agreed, ["priority", "zlib"])

        spy = SpyWriter(client.writer)
        client.writer = spy
        client.send_data("hello extension world", priority=2, compress=True)
        wire_frame = spy.frames[-1]
        self.assertEqual(wire_frame["pri"], 2)
        self.assertEqual(wire_frame["comp"], "zlib")
        self.assertNotIn("hello extension world", wire_frame["payload"])

        received = server.recv_data()
        self.assertEqual(received["payload"], "hello extension world")
        self.assertEqual(received["pri"], 2)
        self.assertNotIn("comp", received)

    def test_no_options_base_frame_still_works(self):
        client, server = make_sessions()
        connect(client, server)
        client.send_data("plain")
        received = server.recv_data()
        self.assertEqual(received["payload"], "plain")
        self.assertNotIn("pri", received)
        self.assertNotIn("comp", received)


class PartialSupportTests(unittest.TestCase):
    def test_partial_overlap_only_intersection_active(self):
        client, server = make_sessions(
            client_kw={"offered": ["priority", "zlib"]},
            server_kw={"offered": ["priority"]},
        )
        connect(client, server)
        self.assertEqual(client.agreed, ["priority"])

        spy = SpyWriter(client.writer)
        client.writer = spy
        client.send_data("prio only", priority=1)
        self.assertEqual(spy.frames[-1].get("pri"), 1)
        self.assertNotIn("comp", spy.frames[-1])

    def test_failed_extension_invisible_at_api_and_wire(self):
        client, server = make_sessions(
            client_kw={"offered": ["priority"]},
            server_kw={"offered": ["priority"]},
        )
        connect(client, server)

        with self.assertRaises(ExtensionNotNegotiated) as cm:
            client.send_data("x", compress=True)
        self.assertEqual(cm.exception.agreed, ["priority"])
        self.assertIn("zlib", str(cm.exception))

        spy = SpyWriter(client.writer)
        client.writer = spy
        client.send_data("no new fields")
        self.assertEqual(set(spy.frames[-1]), {"type", "seq", "payload"})

    def test_completely_unsupported_runs_base(self):
        client, server = make_sessions(
            client_kw={"offered": [], "required": []},
            server_kw={"offered": []},
        )
        connect(client, server)
        self.assertFalse(client.legacy)  # 双方都是新版，只是交集为空
        self.assertEqual(client.agreed, [])
        client.send_data("base only")
        self.assertEqual(server.recv_data()["payload"], "base only")

    def test_bad_priority_value_rejected(self):
        client, server = make_sessions()
        connect(client, server)
        with self.assertRaises(ValueError):
            client.send_data("x", priority=9)


class RequiredTests(unittest.TestCase):
    def test_required_missing_fails_both_sides_with_capabilities(self):
        client, server = make_sessions(
            client_kw={"offered": ["priority", "zlib"], "required": ["zlib"]},
            server_kw={"offered": ["priority"]},
        )
        _, t, server_errors = run_handshake(server)
        with self.assertRaises(NegotiationFailed) as cm:
            client.handshake()
        err = cm.exception
        self.assertEqual(err.required, ["zlib"])
        self.assertEqual(err.supported, ["priority"])
        t.join(timeout=5)
        self.assertEqual(len(server_errors), 1)
        self.assertIsInstance(server_errors[0], NegotiationFailed)
        self.assertEqual(server_errors[0].offered, ["priority", "zlib"])
        self.assertEqual(server_errors[0].agreed, ["priority"])

    def test_unsupported_ack_downgrade_raises(self):
        # 模拟恶意服务端：ack 不包含 required
        client, server = make_sessions(
            client_kw={"offered": ["zlib"], "required": ["zlib"]},
        )

        original = server.writer

        class LyingWriter:
            def __init__(self, inner):
                self.inner = inner
                self.acked = False

            def write(self, data):
                import json
                import struct
                from protoext.wire import read_frame
                frame = read_frame(io.BytesIO(data))
                if frame.get("type") == "hello_ack" and not self.acked:
                    self.acked = True
                    frame["ext"] = []  # 明明支持却声明不支持
                    body = json.dumps(frame).encode()
                    data = struct.pack(">I", len(body)) + body
                self.inner.write(data)

            def flush(self):
                pass

            def close(self):
                self.inner.close()

        server.writer = LyingWriter(original)
        _, t, errors = run_handshake(server)
        with self.assertRaises(NegotiationFailed):
            client.handshake()
        t.join(timeout=5)
        client.close()
        server.close()


class DataPhaseContradictionTests(unittest.TestCase):
    def test_receiving_field_of_unnegotiated_extension_is_contradiction(self):
        import json
        import struct
        client, server = make_sessions(
            client_kw={"offered": ["zlib"]},
            server_kw={"offered": ["zlib"]},
        )
        connect(client, server)
        # 服务端协商结果只有 zlib，却手工发出 priority 扩展字段
        body = json.dumps({"type": "data", "seq": 0,
                           "payload": "x", "pri": 0}).encode()
        server.writer.write(struct.pack(">I", len(body)) + body)
        with self.assertRaises(NegotiationContradiction) as cm:
            client.recv_data()
        self.assertEqual(cm.exception.used, ["priority"])
        self.assertEqual(cm.exception.agreed, ["zlib"])

    def test_send_before_handshake(self):
        client, _ = make_sessions()
        with self.assertRaises(ProtocolError):
            client.send_data("x")


class CloseTests(unittest.TestCase):
    def test_fin_raises_connection_closed(self):
        client, server = make_sessions()
        connect(client, server)
        server.send_fin()
        with self.assertRaises(ConnectionClosed):
            client.recv_data()


class LocalConfigTests(unittest.TestCase):
    def test_required_not_in_offered_contradiction_at_construction(self):
        from tests._harness import pipe_pair
        io_pair, _ = pipe_pair()
        with self.assertRaises(NegotiationContradiction):
            Session(io_pair[0], io_pair[1], "client",
                    offered=["priority"], required=["zlib"])

    def test_unregistered_extension_rejected(self):
        from tests._harness import pipe_pair
        io_pair, _ = pipe_pair()
        with self.assertRaises(ProtocolError):
            Session(io_pair[0], io_pair[1], "client", offered=["bogus"])

    def test_bad_role(self):
        from tests._harness import pipe_pair
        io_pair, _ = pipe_pair()
        with self.assertRaises(ValueError):
            Session(io_pair[0], io_pair[1], "middle")


if __name__ == "__main__":
    unittest.main()
