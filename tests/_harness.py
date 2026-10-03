"""测试辅助：内存双工管道、MITM 帧代理、线程建连。"""

import json
import queue
import struct
import threading


class PipeStream:
    """全双工内存管道的一端流（read(n) / write / close）。"""

    def __init__(self):
        self._q = queue.Queue()
        self._buf = bytearray()
        self._closed = False

    def write(self, data):
        self._q.put(bytes(data))

    def read(self, n):
        while len(self._buf) < n:
            chunk = self._q.get()
            if chunk is None:
                break
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def close(self):
        if not self._closed:
            self._closed = True
            self._q.put(None)


def pipe_pair():
    """返回 (client_io, server_io)，每个 io 为 (reader, writer)。"""
    cbox, sbox = PipeStream(), PipeStream()
    return (cbox, sbox), (sbox, cbox)


def _read_one_frame(stream):
    header = b""
    while len(header) < 4:
        chunk = stream.read(4 - len(header))
        if not chunk:
            return None
        header += chunk
    (length,) = struct.unpack(">I", header)
    body = b""
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            return None
        body += chunk
    return json.loads(body.decode("utf-8"))


def _encode(frame):
    body = json.dumps(frame, ensure_ascii=False).encode("utf-8")
    return struct.pack(">I", len(body)) + body


def mitm_pair(rule):
    """在两端之间插入双向帧代理。

    rule(direction, frame) -> frame|None：None 表示原样转发。
    返回 (client_io, server_io)，两线程在后台搬运。
    """
    (client_io, mid_a) = pipe_pair()
    (mid_b, server_io) = pipe_pair()

    def proxy(src, dst, direction):
        while True:
            frame = _read_one_frame(src)
            if frame is None:
                dst.close()
                return
            replaced = rule(direction, frame)
            dst.write(_encode(replaced if replaced is not None else frame))

    threads = [
        threading.Thread(target=proxy, args=(mid_a[0], mid_b[1], "c2s"),
                         daemon=True),
        threading.Thread(target=proxy, args=(mid_b[0], mid_a[1], "s2c"),
                         daemon=True),
    ]
    for t in threads:
        t.start()
    return client_io, server_io


def run_handshake(server_session):
    """在线程中执行服务端握手，返回 (server_session, thread, errors[])。"""
    errors = []

    def work():
        try:
            server_session.handshake()
        except BaseException as exc:  # noqa: BLE001 - 测试辅助需捕获全部
            errors.append(exc)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    return server_session, t, errors


def make_sessions(client_kw=None, server_kw=None):
    from protoext import Session
    (client_io, server_io) = pipe_pair()
    client = Session(client_io[0], client_io[1], "client", **(client_kw or {}))
    server = Session(server_io[0], server_io[1], "server", **(server_kw or {}))
    return client, server


def connect(client, server):
    """完成双向握手；服务端在线程中运行，其异常会被重新抛出。"""
    _, t, errors = run_handshake(server)
    client.handshake()
    t.join(timeout=5)
    if errors:
        raise errors[0]
    return client, server


class SpyWriter:
    """包装 writer，记录所有写出的帧对象。"""

    def __init__(self, inner):
        self.inner = inner
        self.frames = []

    def write(self, data):
        from protoext.wire import read_frame
        import io
        frame = read_frame(io.BytesIO(data))
        self.frames.append(frame)
        self.inner.write(data)

    def flush(self):
        pass

    def close(self):
        self.inner.close()
