"""旧版本协议实现（协商能力出现之前的版本），用于互操作测试。

特征：
    * hello / hello_ack 不含 ext 字段；握手阶段忽略未知字段（向前兼容）。
    * 数据阶段严格：data 消息中出现任何未知字段即报错 ——
      这正是"直接发新字段会让老对端解析失败"的历史行为。
"""

import os

from . import codec
from .errors import ProtocolError, UnexpectedMessageError, UnknownFieldError

PROTOCOL_VERSION = 1

BASE_DATA_FIELDS = frozenset({"type", "seq", "payload"})


class LegacySession:
    def __init__(self, transport, role):
        if role not in ("client", "server"):
            raise ValueError("role 必须是 'client' 或 'server'")
        self.transport = transport
        self.role = role
        self.handshake_done = False
        self._seq = 0

    def handshake(self):
        if self.role == "client":
            codec.send_message(self.transport, {
                "type": "hello",
                "proto": PROTOCOL_VERSION,
                "nonce": os.urandom(16).hex(),
            })
            ack, _ = codec.recv_message(self.transport)
            if ack.get("type") != "hello_ack":
                raise UnexpectedMessageError(
                    f"期望 hello_ack，收到: {ack.get('type')!r}"
                )
            # 忽略 ack 中的未知字段（如新版 server 的 hello_hash）
        else:
            hello, _ = codec.recv_message(self.transport)
            if hello.get("type") != "hello":
                raise UnexpectedMessageError(
                    f"期望 hello，收到: {hello.get('type')!r}"
                )
            # 忽略 hello 中的未知字段（如新版 client 的 ext）
            codec.send_message(self.transport, {
                "type": "hello_ack",
                "proto": PROTOCOL_VERSION,
                "nonce": os.urandom(16).hex(),
            })
        self.handshake_done = True

    def send_data(self, payload):
        if not self.handshake_done:
            raise ProtocolError("握手未完成")
        msg = {"type": "data", "seq": self._seq, "payload": payload}
        self._seq += 1
        return codec.send_message(self.transport, msg)

    def recv_data(self):
        if not self.handshake_done:
            raise ProtocolError("握手未完成")
        msg, _ = codec.recv_message(self.transport)
        if msg.get("type") != "data":
            raise UnexpectedMessageError(
                f"期望 data，收到: {msg.get('type')!r}"
            )
        unknown = set(msg) - BASE_DATA_FIELDS
        if unknown:
            # 老版本行为：新字段导致解析失败
            raise UnknownFieldError(f"老版本无法解析的新字段: {sorted(unknown)}")
        return msg
