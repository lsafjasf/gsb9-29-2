"""会话状态机：握手协商 + 受协商结果约束的数据收发。

线程模型：一个 Session 服务于一条全双工连接的一端，握手顺序为
client hello -> server hello_ack -> client finish -> server finish。
"""

import hashlib
import secrets

from .errors import (
    ConnectionClosed,
    NegotiationError,
    NegotiationFailed,
    NegotiationContradiction,
    NegotiationTampered,
    ExtensionNotNegotiated,
    ProtocolError,
)
from .extensions import registry as default_registry
from .handshake import negotiate, validate_ack, validate_offer
from .wire import canonical, read_frame, write_frame

PROTO_BASE = 1
PROTO_V2 = 2

# data 帧中属于基础协议、任何扩展都不得认领的字段
_BASE_DATA_KEYS = {"type", "seq", "payload"}


class Session:
    def __init__(self, reader, writer, role, *, extensions=None,
                 offered=None, required=None, name=None, allow_legacy=True):
        if role not in ("client", "server"):
            raise ValueError("role 必须是 'client' 或 'server'")
        self.reader = reader
        self.writer = writer
        self.role = role
        self.registry = extensions or default_registry

        supported = set(self.registry.names())
        if offered is None:
            offered = sorted(supported)
        self.offered, self.required = validate_offer(offered, required or [])
        # 只声明本端注册表内真正实现的扩展
        unknown = [n for n in self.offered if self.registry.get(n) is None]
        if unknown:
            raise ProtocolError(f"声明了未注册的扩展: {unknown}")

        self.name = name or role
        self.allow_legacy = allow_legacy
        self.peer_ext_ack = []      # 对端在握手中声明/应答的扩展
        self.agreed = []            # 本端最终生效的扩展
        self.legacy = False         # True = 按基础协议运行
        self._seq = 0
        self._done_handshake = False

    # ---- 握手 ----------------------------------------------------------

    def handshake(self):
        if self.role == "client":
            self._client_handshake()
        else:
            self._server_handshake()
        self._done_handshake = True
        return self.agreed

    def _hello_frame(self):
        return {
            "type": "hello", "proto": PROTO_V2, "name": self.name,
            "nonce": secrets.token_hex(8),
            "ext": self.offered, "ext_required": self.required,
        }

    @staticmethod
    def _fingerprint(hello, ack):
        h = hashlib.sha256()
        h.update(canonical(hello))
        h.update(b"|")
        h.update(canonical(ack))
        return h.hexdigest()

    def _client_handshake(self):
        hello = self._hello_frame()
        write_frame(self.writer, hello)

        ack = read_frame(self.reader)
        if ack.get("type") == "error":
            raise self._error_from_frame(ack)
        if ack.get("type") != "hello_ack":
            raise ProtocolError(f"期望 hello_ack，收到 {ack.get('type')!r}")

        # 老对端：ack 中不含 ext 字段或 proto<2 => 基础协议，无 finish
        if "ext" not in ack or ack.get("proto", PROTO_BASE) < PROTO_V2:
            if self.required and not self.allow_legacy:
                raise NegotiationFailed(
                    "对端为旧版本（无协商字段），无法满足必需扩展",
                    offered=self.offered, required=self.required,
                    supported=[], agreed=[], peer_capabilities=ack,
                )
            self.legacy = True
            self.agreed = []
            self.peer_ext_ack = []
            return

        self.peer_ext_ack = validate_ack(ack["ext"], self.offered,
                                         supported=self.offered)
        missing = sorted(set(self.required) - set(self.peer_ext_ack))
        if missing:
            raise NegotiationFailed(
                f"对端 ack 未包含必需扩展: {missing}",
                offered=self.offered, required=self.required,
                supported=self.peer_ext_ack, agreed=self.peer_ext_ack,
                peer_capabilities=ack,
            )
        self.agreed = self.peer_ext_ack

        # 握手指纹交换：任何对 ext 字段的篡改/剥离都会导致双方指纹不一致
        write_frame(self.writer, {"type": "finish",
                                  "digest": self._fingerprint(hello, ack)})
        reply = read_frame(self.reader)
        if reply.get("type") == "error":
            raise self._error_from_frame(reply)
        if reply.get("type") != "finish" or \
                reply.get("digest") != self._fingerprint(hello, ack):
            raise NegotiationTampered(
                "服务端握手指纹不一致，协商字段可能被篡改",
                offered=self.offered, required=self.required,
                supported=self.peer_ext_ack, agreed=self.agreed,
                peer_capabilities=reply,
            )

    def _server_handshake(self):
        hello = read_frame(self.reader)
        if hello.get("type") != "hello":
            raise ProtocolError(f"期望 hello，收到 {hello.get('type')!r}")

        # 老对端：hello 中不含 ext 字段 => 按基础协议应答
        if "ext" not in hello:
            self.legacy = True
            self.agreed = []
            write_frame(self.writer, {"type": "hello_ack",
                                      "proto": PROTO_BASE})
            return

        try:
            peer_offered, peer_required = validate_offer(
                hello.get("ext"), hello.get("ext_required"))
        except NegotiationContradiction as exc:
            write_frame(self.writer, {
                "type": "error", "code": "negotiation_contradiction",
                "message": str(exc).splitlines()[0],
                "detail": {"offered": exc.offered,
                           "required": exc.required,
                           "supported": self.offered},
            })
            raise
        self.peer_ext_ack = peer_offered
        try:
            self.agreed = negotiate(peer_offered, peer_required, self.offered)
        except NegotiationFailed as exc:
            detail = {
                "offered": exc.offered, "required": exc.required,
                "supported": exc.supported, "agreed": exc.agreed,
                "missing": sorted(set(exc.required) - set(exc.agreed)),
            }
            write_frame(self.writer, {"type": "error",
                                      "code": "negotiation_failed",
                                      "detail": detail})
            raise

        ack = {"type": "hello_ack", "proto": PROTO_V2,
               "name": self.name, "ext": self.agreed}
        write_frame(self.writer, ack)

        finish = read_frame(self.reader)
        if finish.get("type") == "error":
            raise self._error_from_frame(finish)
        expected = self._fingerprint(hello, ack)
        if finish.get("type") != "finish" or finish.get("digest") != expected:
            write_frame(self.writer, {
                "type": "error", "code": "negotiation_tampered",
                "detail": {"offered": peer_offered,
                           "required": peer_required,
                           "supported": self.offered,
                           "agreed": self.agreed},
            })
            raise NegotiationTampered(
                "客户端握手指纹不一致，协商字段可能被篡改或剥离",
                offered=peer_offered, required=peer_required,
                supported=self.offered, agreed=self.agreed,
                peer_capabilities=finish,
            )
        write_frame(self.writer, {"type": "finish", "digest": expected})

    def _error_from_frame(self, frame):
        detail = frame.get("detail") or {}
        code = frame.get("code", "unknown")
        cls = {
            "negotiation_failed": NegotiationFailed,
            "negotiation_contradiction": NegotiationContradiction,
            "negotiation_tampered": NegotiationTampered,
        }.get(code, ProtocolError)
        return cls(
            f"对端返回协议错误: {code} {frame.get('message', '')}".strip(),
            offered=detail.get("offered", []),
            required=detail.get("required", []),
            supported=detail.get("supported", []),
            agreed=detail.get("agreed", []),
            peer_capabilities=frame,
        )

    # ---- 数据阶段 ------------------------------------------------------

    def has_extension(self, name):
        return name in self.agreed

    def send_data(self, payload, **options):
        if not self._done_handshake:
            raise ProtocolError("握手尚未完成")
        frame = {"type": "data", "seq": self._seq, "payload": payload}
        self._seq += 1

        # API 层不可见性：禁止在未协商的扩展上传递参数
        for opt, value in options.items():
            if value is None:
                continue
            ext_name = "zlib" if opt == "compress" else opt
            if ext_name not in self.agreed:
                raise ExtensionNotNegotiated(
                    f"扩展 {ext_name!r} 未协商成功，禁止使用其选项 {opt!r}",
                    offered=self.offered, required=self.required,
                    supported=self.peer_ext_ack, agreed=self.agreed,
                )

        for name in self.agreed:
            ext = self.registry.get(name)
            ext.encode(frame, {"priority": options.get("priority"),
                               "compress": options.get("compress")})
        write_frame(self.writer, frame)
        return frame["seq"]

    def recv_data(self):
        if not self._done_handshake:
            raise ProtocolError("握手尚未完成")
        frame = read_frame(self.reader)
        if frame.get("type") == "error":
            raise self._error_from_frame(frame)
        if frame.get("type") == "fin":
            raise ConnectionClosed("对端发送 fin，连接关闭")
        if frame.get("type") != "data":
            raise ProtocolError(f"数据阶段收到非法帧: {frame.get('type')!r}")

        # 线路层不可见性：出现了属于“已知但未协商”扩展的字段 => 矛盾
        used = set()
        for key in frame:
            if key in _BASE_DATA_KEYS:
                continue
            owner = self.registry.field_owner(key)
            if owner is not None and owner not in self.agreed:
                used.add(owner)
        if used:
            raise NegotiationContradiction(
                f"对端使用了未协商成功的扩展字段: {sorted(used)}",
                offered=self.offered, required=self.required,
                supported=self.peer_ext_ack, agreed=self.agreed,
                used=used, peer_capabilities=frame,
            )

        for name in self.agreed:
            ext = self.registry.get(name)
            if frame.keys() & ext.frame_fields:
                ext.decode(frame)
        return frame

    def send_fin(self):
        write_frame(self.writer, {"type": "fin"})

    def close(self):
        try:
            self.writer.close()
        finally:
            rclose = getattr(self.reader, "close", None)
            if callable(rclose):
                rclose()


class MemoryPipe:
    """测试用内存全双工管道。"""

    @staticmethod
    def pair():
        import socket
        a, b = socket.socketpair()
        return (a.makefile("rwb", buffering=0), b.makefile("rwb", buffering=0))
