"""测试公共工具：socketpair 驱动双端会话、抓包、MITM 中继。"""

import json
import socket
import struct
import threading

from protoext.codec import SocketTransport
from protoext.session import Session
from protoext.legacy import LegacySession

TIMEOUT = 5


def make_transport(sock):
    return SocketTransport(sock, timeout=TIMEOUT)


class EndpointResult:
    def __init__(self):
        self.session = None
        self.error = None
        self.output = None

    def __repr__(self):
        if self.error is not None:
            return f"<error {type(self.error).__name__}: {self.error}>"
        return f"<ok negotiated={getattr(self.session, 'negotiated', None)}>"


def run_pair(make_client, make_server, client_fn=None, server_fn=None):
    """在 socketpair 上并发驱动两端 handshake + 可选的数据阶段动作。

    make_*: (transport) -> session
    *_fn:   (session) -> 任意返回值（在 handshake 成功后执行）
    返回 {"client": EndpointResult, "server": EndpointResult}
    """
    csock, ssock = socket.socketpair()
    results = {"client": EndpointResult(), "server": EndpointResult()}

    def drive(result, make, sock, fn):
        try:
            session = make(make_transport(sock))
            session.handshake()
            result.session = session
            if fn is not None:
                result.output = fn(session)
        except Exception as exc:  # noqa: BLE001 - 测试需要捕获一切
            result.error = exc
        finally:
            try:
                sock.close()
            except OSError:
                pass

    threads = [
        threading.Thread(target=drive, args=(results["client"], make_client, csock, client_fn)),
        threading.Thread(target=drive, args=(results["server"], make_server, ssock, server_fn)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(TIMEOUT + 5)
        assert not t.is_alive(), "端点线程超时未退出（可能发生死锁）"
    return results


def new_client(extensions=(), required=()):
    return lambda t: Session(t, "client", extensions=extensions, required=required)


def new_server(extensions=(), required=()):
    return lambda t: Session(t, "server", extensions=extensions, required=required)


def legacy_client():
    return lambda t: LegacySession(t, "client")


def legacy_server():
    return lambda t: LegacySession(t, "server")


class RecordingTransport:
    """包装 SocketTransport，记录写出的每一帧（解析为 dict 列表）。"""

    def __init__(self, inner):
        self._inner = inner
        self._buf = bytearray()

    def read_exactly(self, n):
        return self._inner.read_exactly(n)

    def write(self, data):
        self._buf.extend(data)
        self._inner.write(data)

    def sent_frames(self):
        frames = []
        buf = bytes(self._buf)
        pos = 0
        while pos + 4 <= len(buf):
            (length,) = struct.unpack(">I", buf[pos:pos + 4])
            payload = buf[pos + 4:pos + 4 + length]
            frames.append(json.loads(payload))
            pos += 4 + length
        return frames


def run_mitm_pair(make_client, make_server, c2s_transform=None, s2c_transform=None):
    """客户端与服务器之间串一个 MITM 中继，可篡改两个方向的帧。

    transform: (dict) -> dict，返回替换后的消息。
    """
    c1, c2 = socket.socketpair()   # client <-> mitm
    s1, s2 = socket.socketpair()   # mitm <-> server
    for s in (c2, s1):
        s.settimeout(1)

    def relay(reader, writer, transform):
        try:
            while True:
                header = reader.recv(4)
                if len(header) < 4:
                    return
                (length,) = struct.unpack(">I", header)
                payload = b""
                while len(payload) < length:
                    chunk = reader.recv(length - len(payload))
                    if not chunk:
                        return
                    payload += chunk
                if transform is None:
                    out = payload
                else:
                    obj = transform(json.loads(payload))
                    out = json.dumps(obj, sort_keys=True,
                                     separators=(",", ":")).encode()
                writer.sendall(struct.pack(">I", len(out)) + out)
        except (OSError, EOFError):
            return

    relays = [
        threading.Thread(target=relay, args=(c2, s1, c2s_transform), daemon=True),
        threading.Thread(target=relay, args=(s1, c2, s2c_transform), daemon=True),
    ]
    for t in relays:
        t.start()

    results = {"client": EndpointResult(), "server": EndpointResult()}

    def drive(result, make, sock):
        try:
            session = make(make_transport(sock))
            session.handshake()
            result.session = session
        except Exception as exc:  # noqa: BLE001
            result.error = exc
        finally:
            try:
                sock.close()
            except OSError:
                pass

    endpoints = [
        threading.Thread(target=drive, args=(results["client"], make_client, c1)),
        threading.Thread(target=drive, args=(results["server"], make_server, s2)),
    ]
    for t in endpoints:
        t.start()
    for t in endpoints:
        t.join(TIMEOUT + 5)
        assert not t.is_alive(), "端点线程超时未退出"
    for s in (c1, c2, s1, s2):
        try:
            s.close()
        except OSError:
            pass
    return results
