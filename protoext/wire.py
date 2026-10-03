"""线路格式：4 字节大端长度前缀 + UTF-8 JSON 帧。"""

import json
import struct

from .errors import ProtocolError

MAX_FRAME_SIZE = 1 << 20  # 1 MiB

BASE_FRAME_KEYS = {"type", "proto", "name", "seq", "payload", "nonce",
                   "ext", "ext_required", "digest", "code", "detail"}


def canonical(obj):
    """确定性 JSON 序列化，用于握手指纹。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def encode_frame(obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_FRAME_SIZE:
        raise ProtocolError(f"帧过大: {len(body)} 字节")
    return struct.pack(">I", len(body)) + body


def _read_exactly(reader, n):
    buf = b""
    while len(buf) < n:
        chunk = reader.read(n - len(buf))
        if not chunk:
            raise EOFError("连接在帧中途关闭")
        buf += chunk
    return buf


def read_frame(reader):
    """从类文件对象（有 read(n)）读取一帧并解析为 dict。"""
    header = _read_exactly(reader, 4)
    (length,) = struct.unpack(">I", header)
    if length > MAX_FRAME_SIZE:
        raise ProtocolError(f"帧长度超限: {length} > {MAX_FRAME_SIZE}")
    body = _read_exactly(reader, length)
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"帧不是合法 JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError(f"帧必须是 JSON 对象，得到: {type(obj).__name__}")
    if "type" not in obj or not isinstance(obj["type"], str):
        raise ProtocolError("帧缺少字符串类型的 'type' 字段")
    return obj


def write_frame(writer, obj):
    data = encode_frame(obj)
    writer.write(data)
    flush = getattr(writer, "flush", None)
    if callable(flush):
        flush()
