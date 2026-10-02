"""帧编解码与传输抽象。

帧格式：4 字节大端长度 + UTF-8 JSON 对象。
握手转录（transcript）使用线上原始字节，保证篡改可被检出。
"""

import json
import struct

from .errors import FrameTooLargeError, MalformedMessageError

MAX_FRAME_SIZE = 1 << 20  # 1 MiB

_HEADER = struct.Struct(">I")


class SocketTransport:
    """基于阻塞 socket 的传输。timeout 为秒，超时由 socket 抛 timeout。"""

    def __init__(self, sock, timeout=None):
        self._sock = sock
        if timeout is not None:
            sock.settimeout(timeout)

    def read_exactly(self, n):
        chunks = []
        remaining = n
        while remaining > 0:
            chunk = self._sock.recv(remaining)
            if not chunk:
                raise EOFError("连接被对端关闭")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def write(self, data):
        self._sock.sendall(data)


def encode_payload(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def send_message(transport, obj):
    """编码并发送一个消息，返回线上原始 payload 字节（供转录使用）。"""
    payload = encode_payload(obj)
    transport.write(_HEADER.pack(len(payload)) + payload)
    return payload


def recv_message(transport, max_size=MAX_FRAME_SIZE):
    """接收一个消息，返回 (对象, 线上原始 payload 字节)。"""
    header = transport.read_exactly(_HEADER.size)
    (length,) = _HEADER.unpack(header)
    if length > max_size:
        raise FrameTooLargeError(f"帧长度 {length} 超过上限 {max_size}")
    payload = transport.read_exactly(length)
    try:
        obj = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MalformedMessageError(f"帧内容不是合法 JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise MalformedMessageError("消息必须是 JSON 对象")
    return obj, payload
