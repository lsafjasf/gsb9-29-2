"""会话：握手协商 + 数据收发。

握手流程（新版对新版）：
    client -> hello{proto, nonce, ext[], ext_required[]?}
    server -> hello_ack{proto, nonce, ext[], hello_hash}
    client -> finished{verify}
    server -> finished{verify}

兼容规则：
    * hello 缺少 "ext" 键      -> 对端是老版本，按基础协议运行，跳过 finished。
    * hello_ack 缺少 "ext" 键  -> 对端是老版本，按基础协议运行，跳过 finished。
    * 新版 server 始终在 ack 中携带 hello_hash（所见 hello 的 SHA-256），
      老版本 client 会忽略该未知握手字段，新版本 client 用它检测降级篡改。

未协商成功的扩展在数据面完全不可见：
    * 发送侧：send_data 拒绝携带未协商扩展的字段（ExtensionNotNegotiatedError）。
    * 接收侧：recv_data 拒绝未协商扩展的字段（UnnegotiatedExtensionError），
      以及任何无法归属的未知字段（UnknownFieldError）。
"""

import hashlib
import os

from . import codec
from .errors import (
    ContradictionError,
    ExtensionNotNegotiatedError,
    NegotiationFailedError,
    ProtocolError,
    TamperDetectedError,
    UnexpectedMessageError,
    UnknownFieldError,
    UnnegotiatedExtensionError,
)
from .registry import EXTENSIONS, extension_of_field, fields_of, known_extension

PROTOCOL_VERSION = 1

BASE_DATA_FIELDS = frozenset({"type", "seq", "payload"})

_FINISHED_CONTEXT = b"protoext-finished-v1"
_HELLO_HASH_CONTEXT = b"protoext-hello-hash-v1"


def _validate_ext_list(value, field_name, local_caps, remote_caps):
    if not isinstance(value, list) or not all(isinstance(e, str) for e in value):
        raise ContradictionError(
            f"握手字段 '{field_name}' 必须是字符串数组",
            local_caps=local_caps,
            remote_caps=remote_caps,
        )
    # 去重并保持声明顺序
    seen = []
    for name in value:
        if name not in seen:
            seen.append(name)
    return seen


class Session:
    """一个协议端点。role 为 "client" 或 "server"。"""

    def __init__(self, transport, role, extensions=(), required=()):
        if role not in ("client", "server"):
            raise ValueError("role 必须是 'client' 或 'server'")
        unknown = [e for e in extensions if not known_extension(e)]
        if unknown:
            raise ValueError(f"未知扩展（注册表中不存在）: {unknown}")
        if not set(required) <= set(extensions):
            raise ValueError("required 必须是 extensions 的子集")
        self.transport = transport
        self.role = role
        self.supported = frozenset(extensions)
        self.required = frozenset(required)
        self.negotiated = frozenset()
        self.remote_caps = frozenset()
        self.peer_legacy = False
        self.handshake_done = False
        self._transcript = []
        self._seq = 0

    # ------------------------------------------------------------------ 握手

    def handshake(self):
        if self.handshake_done:
            raise ProtocolError("握手已完成，不能重复执行")
        if self.role == "client":
            self._client_handshake()
        else:
            self._server_handshake()
        self.handshake_done = True
        return self.negotiated

    def _hello_msg(self):
        msg = {
            "type": "hello",
            "proto": PROTOCOL_VERSION,
            "nonce": os.urandom(16).hex(),
            "ext": sorted(self.supported),
        }
        if self.required:
            msg["ext_required"] = sorted(self.required)
        return msg

    def _finished_msg(self):
        digest = hashlib.sha256(
            _FINISHED_CONTEXT + b"".join(self._transcript)
        ).hexdigest()
        return {"type": "finished", "verify": digest}

    def _verify_finished(self, msg):
        if msg.get("type") != "finished" or not isinstance(msg.get("verify"), str):
            raise UnexpectedMessageError(
                f"握手阶段期望 finished 消息，收到: {msg.get('type')!r}"
            )
        expected = hashlib.sha256(
            _FINISHED_CONTEXT + b"".join(self._transcript)
        ).hexdigest()
        if msg["verify"] != expected:
            raise TamperDetectedError(
                "握手转录校验失败：双方看到的协商内容不一致，"
                "协商字段疑似被中间人篡改"
            )

    def _client_handshake(self):
        hello = self._hello_msg()
        self._transcript.append(codec.send_message(self.transport, hello))

        ack, raw = codec.recv_message(self.transport)
        if ack.get("type") == "error":
            self._raise_remote_error(ack)
        if ack.get("type") != "hello_ack":
            raise UnexpectedMessageError(
                f"握手阶段期望 hello_ack，收到: {ack.get('type')!r}"
            )
        if ack.get("proto") != PROTOCOL_VERSION:
            raise ProtocolError(f"协议版本不兼容: {ack.get('proto')!r}")
        self._transcript.append(raw)

        has_hash = isinstance(ack.get("hello_hash"), str)
        if has_hash:
            # 对端是新版本：校验它看到的 hello 与我们发出的完全一致
            expected = hashlib.sha256(
                _HELLO_HASH_CONTEXT + self._transcript[0]
            ).hexdigest()
            if ack["hello_hash"] != expected:
                raise TamperDetectedError(
                    "对端收到的 hello 与本端发出的不一致，"
                    "协商字段疑似被中间人篡改"
                )
        if "ext" not in ack:
            if has_hash:
                # 新版对端不可能省略 ext：ext 被剥离，属于降级篡改
                raise TamperDetectedError(
                    "hello_ack 缺少 ext 但携带 hello_hash，"
                    "协商字段疑似被中间人剥离"
                )
            # 老版本对端：基础协议，无 finished 交换
            self.peer_legacy = True
            self.negotiated = frozenset()
            return

        accepted = _validate_ext_list(ack["ext"], "ext", self.supported, ())
        self.remote_caps = frozenset(accepted)
        extra = set(accepted) - self.supported
        if extra:
            raise ContradictionError(
                f"对端接受了本端从未提供的扩展: {sorted(extra)}",
                local_caps=self.supported,
                remote_caps=accepted,
            )
        missing = self.required - set(accepted)
        if missing:
            raise NegotiationFailedError(
                "对端未接受本端必需的扩展",
                local_caps=self.supported,
                remote_caps=accepted,
                missing=missing,
            )
        self.negotiated = frozenset(accepted)

        codec.send_message(self.transport, self._finished_msg())
        fin, _ = codec.recv_message(self.transport)
        if fin.get("type") == "error":
            self._raise_remote_error(fin)
        self._verify_finished(fin)

    def _server_handshake(self):
        hello, raw = codec.recv_message(self.transport)
        if hello.get("type") != "hello":
            raise UnexpectedMessageError(
                f"握手阶段期望 hello，收到: {hello.get('type')!r}"
            )
        if hello.get("proto") != PROTOCOL_VERSION:
            raise ProtocolError(f"协议版本不兼容: {hello.get('proto')!r}")
        self._transcript.append(raw)

        ack = {
            "type": "hello_ack",
            "proto": PROTOCOL_VERSION,
            "nonce": os.urandom(16).hex(),
            # 始终携带：让新版 client 能检测 hello 是否被篡改；
            # 老版本 client 会忽略这个未知握手字段。
            "hello_hash": hashlib.sha256(
                _HELLO_HASH_CONTEXT + raw
            ).hexdigest(),
        }

        if "ext" not in hello:
            # 老版本对端：基础协议，ack 不带 ext，跳过 finished
            self.peer_legacy = True
            self.negotiated = frozenset()
            codec.send_message(self.transport, ack)
            return

        try:
            offered = _validate_ext_list(hello["ext"], "ext", self.supported, ())
            required = _validate_ext_list(
                hello.get("ext_required", []), "ext_required",
                self.supported, offered,
            )
            self.remote_caps = frozenset(offered)
            if not set(required) <= set(offered):
                raise ContradictionError(
                    f"对端要求了未提供的扩展: "
                    f"{sorted(set(required) - set(offered))}",
                    local_caps=self.supported,
                    remote_caps=offered,
                )
        except ContradictionError as exc:
            try:
                codec.send_message(self.transport, {
                    "type": "error",
                    "code": "contradiction",
                    "detail": str(exc),
                })
            except OSError:
                pass  # 对端已断开，错误仍在本端抛出
            raise

        accepted = [e for e in offered if e in self.supported]
        missing = set(required) - set(accepted)
        if missing:
            error_msg = {
                "type": "error",
                "code": "negotiation_failed",
                "detail": "缺少必需的扩展",
                "missing": sorted(missing),
                "server_ext": sorted(self.supported),
            }
            codec.send_message(self.transport, error_msg)
            raise NegotiationFailedError(
                "对端必需的扩展本端不支持",
                local_caps=self.supported,
                remote_caps=offered,
                missing=missing,
            )

        ack["ext"] = accepted
        self.negotiated = frozenset(accepted)
        self._transcript.append(codec.send_message(self.transport, ack))

        fin_msg, _ = codec.recv_message(self.transport)
        if fin_msg.get("type") == "error":
            self._raise_remote_error(fin_msg)
        try:
            self._verify_finished(fin_msg)
        except TamperDetectedError as exc:
            codec.send_message(self.transport, {
                "type": "error",
                "code": "tamper_detected",
                "detail": str(exc),
            })
            raise
        codec.send_message(self.transport, self._finished_msg())

    def _raise_remote_error(self, msg):
        code = msg.get("code", "unknown")
        detail = msg.get("detail", "")
        remote_caps = msg.get("server_ext", self.remote_caps)
        if code == "negotiation_failed":
            raise NegotiationFailedError(
                f"对端拒绝协商: {detail}",
                local_caps=self.supported,
                remote_caps=remote_caps,
                missing=msg.get("missing", ()),
            )
        if code == "tamper_detected":
            raise TamperDetectedError(f"对端报告握手转录不一致: {detail}")
        if code == "contradiction":
            raise ContradictionError(
                f"对端报告声明矛盾: {detail}",
                local_caps=self.supported,
                remote_caps=remote_caps,
            )
        raise ProtocolError(f"对端返回错误 {code!r}: {detail}")

    # ------------------------------------------------------------------ 数据

    def _require_handshake(self):
        if not self.handshake_done:
            raise ProtocolError("握手未完成，不能收发数据")

    def send_data(self, payload, ext=None):
        """发送数据。ext 为 {扩展名: {字段: 值}}，仅允许已协商的扩展。"""
        self._require_handshake()
        msg = {"type": "data", "seq": self._seq, "payload": payload}
        self._seq += 1
        for name, fields in (ext or {}).items():
            if name not in EXTENSIONS:
                raise ExtensionNotNegotiatedError(
                    f"扩展 {name!r} 不在注册表中，拒绝发送"
                )
            if name not in self.negotiated:
                raise ExtensionNotNegotiatedError(
                    f"扩展 {name!r} 未协商成功，其字段对本端完全不可见，拒绝发送"
                )
            unknown = set(fields) - fields_of(name)
            if unknown:
                raise UnknownFieldError(
                    f"字段 {sorted(unknown)} 不属于扩展 {name!r}"
                )
            msg.update(fields)
        return codec.send_message(self.transport, msg)

    def recv_data(self):
        """接收数据。严格校验：未协商扩展字段与未知字段一律拒绝。"""
        self._require_handshake()
        msg, _ = codec.recv_message(self.transport)
        if msg.get("type") == "error":
            self._raise_remote_error(msg)
        if msg.get("type") != "data":
            raise UnexpectedMessageError(
                f"数据阶段期望 data，收到: {msg.get('type')!r}"
            )
        for key in msg:
            if key in BASE_DATA_FIELDS:
                continue
            owner = extension_of_field(key)
            if owner is None:
                raise UnknownFieldError(f"收到无法归属的未知字段: {key!r}")
            if owner not in self.negotiated:
                raise UnnegotiatedExtensionError(
                    f"收到字段 {key!r}，但其所属扩展 {owner!r} 未协商成功"
                )
        return msg
