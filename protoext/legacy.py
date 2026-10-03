"""旧版（v1）协议端点：完全不认识协商字段。

用于互操作测试与演示：它收/发的所有帧都不含 ext 字段，
看到新对端多出的协商字段时应按 JSON 未知属性忽略（但本实现严格，
只解析它认识的键）。
"""

from .errors import ConnectionClosed, ProtocolError
from .wire import read_frame, write_frame


class LegacySession:
    def __init__(self, reader, writer, role, *, name="legacy"):
        self.reader = reader
        self.writer = writer
        self.role = role
        self.name = name
        self._seq = 0
        self.done = False

    def handshake(self):
        if self.role == "client":
            write_frame(self.writer, {"type": "hello", "proto": 1,
                                      "name": self.name})
            ack = read_frame(self.reader)
            if ack.get("type") != "hello_ack":
                raise ProtocolError("旧协议握手失败")
        else:
            hello = read_frame(self.reader)
            if hello.get("type") != "hello":
                raise ProtocolError("旧协议握手失败")
            write_frame(self.writer, {"type": "hello_ack", "proto": 1})
        self.done = True
        return []  # 永远没有协商扩展

    def send_data(self, payload):
        frame = {"type": "data", "seq": self._seq, "payload": payload}
        self._seq += 1
        write_frame(self.writer, frame)
        return frame["seq"]

    def recv_data(self):
        frame = read_frame(self.reader)
        if frame.get("type") == "fin":
            raise ConnectionClosed("对端关闭")
        if frame.get("type") != "data":
            raise ProtocolError("非法数据帧")
        # 旧版只取认识的键，其余忽略
        return {"type": "data", "seq": frame["seq"], "payload": frame["payload"]}

    def send_fin(self):
        write_frame(self.writer, {"type": "fin"})

    def close(self):
        try:
            self.writer.close()
        finally:
            close = getattr(self.reader, "close", None)
            if callable(close):
                close()
