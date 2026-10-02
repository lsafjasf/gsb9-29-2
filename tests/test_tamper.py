"""协商字段被篡改的情形：MITM 修改 hello / hello_ack 必须被检出。"""

import unittest

from protoext.errors import ContradictionError, TamperDetectedError

from util import new_client, new_server, run_mitm_pair

ALL = ["compression", "priority"]


def strip_hello_ext(msg):
    if msg.get("type") == "hello":
        msg = dict(msg)
        msg.pop("ext", None)
        msg.pop("ext_required", None)
    return msg


def rewrite_hello_ext(msg):
    if msg.get("type") == "hello" and "ext" in msg:
        msg = dict(msg)
        msg["ext"] = ["priority"]  # 悄悄删掉 compression
    return msg


def strip_ack_ext(msg):
    if msg.get("type") == "hello_ack":
        msg = dict(msg)
        msg.pop("ext", None)
    return msg


def strip_ack_ext_and_hash(msg):
    if msg.get("type") == "hello_ack":
        msg = dict(msg)
        msg.pop("ext", None)
        msg.pop("hello_hash", None)
    return msg


def inflate_ack_ext(msg):
    if msg.get("type") == "hello_ack" and "ext" in msg:
        msg = dict(msg)
        msg["ext"] = list(msg["ext"]) + ["compression-plus"]
    return msg


class TamperTests(unittest.TestCase):

    def test_strip_ext_from_hello_detected(self):
        # MITM 剥离 hello 的 ext：server 进入 legacy 模式但仍带 hello_hash，
        # client 发现"有 hash 却无 ext" -> 篡改
        result = run_mitm_pair(
            new_client(ALL), new_server(ALL), c2s_transform=strip_hello_ext
        )
        self.assertIsInstance(result["client"].error, TamperDetectedError)

    def test_rewrite_ext_in_hello_detected(self):
        # MITM 修改 hello 的 ext 内容：hello_hash 校验失败
        result = run_mitm_pair(
            new_client(ALL), new_server(ALL), c2s_transform=rewrite_hello_ext
        )
        self.assertIsInstance(result["client"].error, TamperDetectedError)

    def test_strip_ext_from_ack_detected(self):
        # MITM 剥离 ack 的 ext 但保留 hello_hash -> 篡改
        result = run_mitm_pair(
            new_client(ALL), new_server(ALL), s2c_transform=strip_ack_ext
        )
        self.assertIsInstance(result["client"].error, TamperDetectedError)

    def test_inflate_ack_ext_detected_as_contradiction(self):
        # MITM 在 ack 中注入客户端未提供的扩展 -> 矛盾声明
        result = run_mitm_pair(
            new_client(ALL), new_server(ALL), s2c_transform=inflate_ack_ext
        )
        self.assertIsInstance(result["client"].error, ContradictionError)

    def test_full_downgrade_strips_both_sides(self):
        # MITM 同时剥离 hello.ext 与 ack.{ext,hello_hash}（完全降级）：
        # 双方一致地回落到基础协议 —— 扩展不可见但也不会被注入，
        # 与"老对端"情形行为一致。这是已知的残余风险（见 README），
        # 攻击者最多只能把会话降级为基础协议，无法注入扩展字段。
        result = run_mitm_pair(
            new_client(ALL), new_server(ALL),
            c2s_transform=strip_hello_ext,
            s2c_transform=strip_ack_ext_and_hash,
        )
        self.assertIsNone(result["client"].error)
        self.assertIsNone(result["server"].error)
        self.assertEqual(result["client"].session.negotiated, frozenset())
        self.assertEqual(result["server"].session.negotiated, frozenset())
        self.assertTrue(result["client"].session.peer_legacy)
        self.assertTrue(result["server"].session.peer_legacy)

    def test_untampered_handshake_still_works(self):
        # 对照组：MITM 不篡改时握手正常
        result = run_mitm_pair(new_client(ALL), new_server(ALL))
        self.assertIsNone(result["client"].error)
        self.assertIsNone(result["server"].error)
        self.assertEqual(result["client"].session.negotiated, set(ALL))


if __name__ == "__main__":
    unittest.main()
