"""边界用例：畸形帧、超大帧、未知字段、重复声明、连接中断等。"""

import json
import struct
import unittest

from protoext import Session, ProtocolError
from tests._harness import make_sessions, connect, pipe_pair, run_handshake


def raw_frame(obj_or_bytes):
    if isinstance(obj_or_bytes, (bytes, bytearray)):
        body = bytes(obj_or_bytes)
    else:
        body = json.dumps(obj_or_bytes).encode("utf-8")
    return struct.pack(">I", len(body)) + body


class MalformedFrameTests(unittest.TestCase):
    def _client_in_handshake(self):
        (client_io, server_io) = pipe_pair()
        client = Session(client_io[0], client_io[1], "client")
        server = Session(server_io[0], server_io[1], "server")
        return client, server, client_io, server_io

    def test_payload_not_json(self):
        client, server, cio, sio = self._client_in_handshake()
        _, t, errs = run_handshake(server)
        client.writer.write(struct.pack(">I", 4) + b"xxxx")
        t.join(5)
        self.assertEqual(len(errs), 1)
        self.assertIsInstance(errs[0], ProtocolError)

    def test_frame_not_object(self):
        client, server, *_ = self._client_in_handshake()
        _, t, errs = run_handshake(server)
        client.writer.write(raw_frame([1, 2, 3]))
        t.join(5)
        self.assertIsInstance(errs[0], ProtocolError)

    def test_frame_missing_type(self):
        client, server, *_ = self._client_in_handshake()
        _, t, errs = run_handshake(server)
        client.writer.write(raw_frame({"hello": "?"}))
        t.join(5)
        self.assertIsInstance(errs[0], ProtocolError)

    def test_oversized_length_prefix(self):
        client, server, *_ = self._client_in_handshake()
        _, t, errs = run_handshake(server)
        client.writer.write(struct.pack(">I", (1 << 20) + 1))
        t.join(5)
        self.assertIsInstance(errs[0], ProtocolError)
        self.assertIn("超限", str(errs[0]))

    def test_connection_closed_during_handshake(self):
        (client_io, server_io) = pipe_pair()
        client = Session(client_io[0], client_io[1], "client")
        server_io[1].close()
        with self.assertRaises(EOFError):
            client.handshake()

    def test_unexpected_frame_type_in_handshake(self):
        client, server, *_ = self._client_in_handshake()
        _, t, errs = run_handshake(server)
        client.writer.write(raw_frame({"type": "data", "seq": 0}))
        t.join(5)
        self.assertIsInstance(errs[0], ProtocolError)


class DataPhaseEdgeTests(unittest.TestCase):
    def test_unknown_future_field_is_ignored(self):
        # 前向兼容：未注册字段（未来扩展）不影响基础字段解析
        client, server = make_sessions(
            client_kw={"offered": []}, server_kw={"offered": []})
        connect(client, server)
        server.writer.write(raw_frame({"type": "data", "seq": 0,
                                       "payload": "future",
                                       "x_new_field": {"a": 1}}))
        frame = client.recv_data()
        self.assertEqual(frame["payload"], "future")
        self.assertEqual(frame["x_new_field"], {"a": 1})

    def test_bad_pri_value_from_peer(self):
        client, server = make_sessions()
        connect(client, server)
        server.writer.write(raw_frame({"type": "data", "seq": 0,
                                       "payload": "x", "pri": 99}))
        with self.assertRaises(ProtocolError):
            client.recv_data()

    def test_comp_without_negotiation_is_contradiction(self):
        client, server = make_sessions(
            client_kw={"offered": ["priority"]},
            server_kw={"offered": ["priority"]})
        connect(client, server)
        server.writer.write(raw_frame({"type": "data", "seq": 0,
                                       "payload": "x", "comp": "zlib"}))
        from protoext import NegotiationContradiction
        with self.assertRaises(NegotiationContradiction):
            client.recv_data()

    def test_duplicate_extension_names_normalized(self):
        client, server = make_sessions(
            client_kw={"offered": ["zlib", "zlib", "priority"]})
        connect(client, server)
        self.assertEqual(client.agreed, ["priority", "zlib"])
        self.assertEqual(server.agreed, ["priority", "zlib"])

    def test_empty_offer_still_v2_handshake(self):
        client, server = make_sessions(
            client_kw={"offered": []}, server_kw={"offered": []})
        connect(client, server)
        # 双方都是新版（有 finish 握手），只是交集为空，不是 legacy
        self.assertFalse(client.legacy)
        self.assertFalse(server.legacy)

    def test_recv_before_handshake(self):
        client, _ = make_sessions()
        with self.assertRaises(ProtocolError):
            client.recv_data()

    def test_garbage_compressed_payload(self):
        import base64
        client, server = make_sessions()
        connect(client, server)
        server.writer.write(raw_frame({
            "type": "data", "seq": 0,
            "payload": base64.b64encode(b"not-zlib").decode(),
            "comp": "zlib"}))
        with self.assertRaises(ProtocolError):
            client.recv_data()


if __name__ == "__main__":
    unittest.main()
