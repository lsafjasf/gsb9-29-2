"""互操作矩阵：新/旧协议四种组合 + 支持度三档。"""

import unittest

from protoext import Session, NegotiationFailed
from protoext.legacy import LegacySession
from tests._harness import pipe_pair, run_handshake


def build_pair(client_factory, server_factory):
    (client_io, server_io) = pipe_pair()
    return client_factory(*client_io), server_factory(*server_io)


def new_client(reader, writer, **kw):
    return Session(reader, writer, "client", **kw)


def new_server(reader, writer, **kw):
    return Session(reader, writer, "server", **kw)


def legacy_client(reader, writer):
    return LegacySession(reader, writer, "client")


def legacy_server(reader, writer):
    return LegacySession(reader, writer, "server")


class InteropMatrixTests(unittest.TestCase):
    # 1) 新版 <-> 新版：全部支持
    def test_new_new_full(self):
        c, s = build_pair(new_client, new_server)
        _, t, errs = run_handshake(s)
        c.handshake()
        t.join(5)
        self.assertEqual(errs, [])
        self.assertEqual(c.agreed, ["priority", "zlib"])
        self.assertFalse(c.legacy)
        c.send_data("full", priority=3, compress=True)
        self.assertEqual(s.recv_data()["payload"], "full")

    # 2) 新版 <-> 新版：部分支持（交集生效）
    def test_new_new_partial(self):
        c, s = build_pair(
            lambda r, w: new_client(r, w, offered=["priority", "zlib"]),
            lambda r, w: new_server(r, w, offered=["zlib"]),
        )
        _, t, errs = run_handshake(s)
        c.handshake()
        t.join(5)
        self.assertEqual(errs, [])
        self.assertEqual(c.agreed, ["zlib"])
        c.send_data("compressed", compress=True)
        frame = s.recv_data()
        self.assertEqual(frame["payload"], "compressed")
        self.assertNotIn("pri", frame)

    # 3) 新版 <-> 新版：完全不支持（交集为空，基础协议运行）
    def test_new_new_none(self):
        c, s = build_pair(
            lambda r, w: new_client(r, w, offered=[]),
            lambda r, w: new_server(r, w, offered=[]),
        )
        _, t, errs = run_handshake(s)
        c.handshake()
        t.join(5)
        self.assertEqual(errs, [])
        self.assertEqual(c.agreed, [])
        self.assertFalse(c.legacy)
        c.send_data("base")
        self.assertEqual(s.recv_data()["payload"], "base")

    # 4) 旧客户端 <-> 新服务端：hello 无 ext => 基础协议
    def test_legacy_client_new_server(self):
        c, s = build_pair(legacy_client, new_server)
        _, t, errs = run_handshake(s)
        c.handshake()
        t.join(5)
        self.assertEqual(errs, [])
        self.assertTrue(s.legacy)
        self.assertEqual(s.agreed, [])
        c.send_data("old talk")
        frame = s.recv_data()
        self.assertEqual(frame["payload"], "old talk")
        self.assertNotIn("pri", frame)

    # 5) 新客户端 <-> 旧服务端：ack 无 ext => 基础协议
    def test_new_client_legacy_server(self):
        c, s = build_pair(new_client, legacy_server)
        _, t, errs = run_handshake(s)
        c.handshake()
        t.join(5)
        self.assertEqual(errs, [])
        self.assertTrue(c.legacy)
        self.assertEqual(c.agreed, [])
        c.send_data("new talks old")
        self.assertEqual(s.recv_data()["payload"], "new talks old")

    # 6) 旧客户端 <-> 旧服务端
    def test_legacy_legacy(self):
        c, s = build_pair(legacy_client, legacy_server)
        _, t, errs = run_handshake(s)
        c.handshake()
        t.join(5)
        self.assertEqual(errs, [])
        c.send_data("both old")
        self.assertEqual(s.recv_data()["payload"], "both old")

    # 7) 新客户端带必需扩展 <-> 旧服务端：默认允许降级；禁止降级时报错
    def test_required_ext_vs_legacy_server(self):
        (cio, sio) = pipe_pair()
        strict = Session(cio[0], cio[1], "client", required=["zlib"],
                         allow_legacy=False)
        legacy = LegacySession(sio[0], sio[1], "server")
        _, t, errs = run_handshake(legacy)
        with self.assertRaises(NegotiationFailed) as cm:
            strict.handshake()
        t.join(5)
        self.assertEqual(cm.exception.required, ["zlib"])
        self.assertEqual(cm.exception.supported, [])

        (cio2, sio2) = pipe_pair()
        tolerant = Session(cio2[0], cio2[1], "client", required=["zlib"])
        legacy2 = LegacySession(sio2[0], sio2[1], "server")
        _, t2, errs2 = run_handshake(legacy2)
        tolerant.handshake()
        t2.join(5)
        self.assertTrue(tolerant.legacy)


if __name__ == "__main__":
    unittest.main()
