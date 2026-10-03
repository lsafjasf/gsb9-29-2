"""协商字段篡改：通过双向 MITM 帧代理修改握手内容。"""

import unittest

from protoext import Session, NegotiationTampered, NegotiationContradiction
from tests._harness import mitm_pair, run_handshake


def tamper_rule(direction, frame):
    """按用例注入的替换规则（模块级，避免闭包序列化问题）。"""
    return _RULE(direction, frame)


_RULE = None


def run_case(rule, client_kw=None, server_kw=None):
    global _RULE
    _RULE = rule
    client_io, server_io = mitm_pair(tamper_rule)
    client = Session(client_io[0], client_io[1], "client", **(client_kw or {}))
    server = Session(server_io[0], server_io[1], "server", **(server_kw or {}))
    _, t, server_errors = run_handshake(server)
    client_err = None
    try:
        client.handshake()
    except BaseException as exc:  # noqa: BLE001
        client_err = exc
    client.close()
    server.close()
    t.join(timeout=5)
    return client_err, (server_errors[0] if server_errors else None)


class TamperTests(unittest.TestCase):
    def test_hello_ext_modified_detected_by_fingerprint(self):
        def rule(direction, frame):
            if direction == "c2s" and frame.get("type") == "hello":
                frame = dict(frame)
                frame["ext"] = ["priority"]  # 去掉 zlib，但保留 ext 字段
            return frame
        client_err, server_err = run_case(rule)
        self.assertIsInstance(server_err, NegotiationTampered)
        self.assertIsInstance(client_err, NegotiationTampered)

    def test_hello_ext_type_corrupted(self):
        def rule(direction, frame):
            if direction == "c2s" and frame.get("type") == "hello":
                frame = dict(frame)
                frame["ext"] = "priority,zlib"  # 类型被破坏
            return frame
        client_err, server_err = run_case(rule)
        self.assertIsInstance(server_err, NegotiationContradiction)
        # 服务端在协商阶段即拒绝；客户端读到 error 帧
        self.assertIsNotNone(client_err)

    def test_ack_ext_downgraded_detected(self):
        def rule(direction, frame):
            if direction == "s2c" and frame.get("type") == "hello_ack":
                frame = dict(frame)
                frame["ext"] = ["priority"]  # 原本应为 [priority, zlib]
            return frame
        client_err, server_err = run_case(rule)
        self.assertIsInstance(client_err, NegotiationTampered)
        self.assertIsInstance(server_err, NegotiationTampered)

    def test_ack_ext_adds_unoffered(self):
        def rule(direction, frame):
            if direction == "s2c" and frame.get("type") == "hello_ack":
                frame = dict(frame)
                frame["ext"] = ["priority", "zlib", "bogus"]
            return frame
        client_err, server_err = run_case(rule)
        self.assertIsInstance(client_err, NegotiationContradiction)
        self.assertIn("从未提供", str(client_err))

    def test_finish_digest_rewritten_still_detected(self):
        # 攻击者改了 hello 后重算 finish 指纹也无法通过：
        # 双方看到的 ack/hello 不同，服务端自己算的指纹对不上
        import hashlib
        from protoext.wire import canonical

        state = {"hello": None, "ack": None}

        def rule(direction, frame):
            if direction == "c2s" and frame.get("type") == "hello":
                frame = dict(frame)
                frame["ext"] = ["priority"]
                state["hello"] = frame
            if direction == "s2c" and frame.get("type") == "hello_ack":
                state["ack"] = frame
            if direction == "c2s" and frame.get("type") == "finish":
                # 攻击者按自己看到的 hello/ack 重算指纹，但服务端的视图不同
                if state["hello"] is not None and state["ack"] is not None:
                    d = hashlib.sha256()
                    d.update(canonical(state["hello"]))
                    d.update(b"|")
                    d.update(canonical(state["ack"]))
                    frame = dict(frame)
                    frame["digest"] = d.hexdigest()
            return frame
        client_err, server_err = run_case(rule)
        # 攻击者可骗过“看到篡改后视图”的一端，但无法同时骗过两端：
        # 客户端按原始 hello 计算指纹，必然发现不一致。
        self.assertIsInstance(client_err, NegotiationTampered)


if __name__ == "__main__":
    unittest.main()
