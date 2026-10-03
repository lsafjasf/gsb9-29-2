"""protoext: 带扩展协商能力的简单消息协议库（仅标准库）。

握手阶段协商扩展集合，协商结果决定数据帧的字段与行为；
未协商成功的扩展在线路上与 API 层面完全不可见。
"""

from .errors import (
    ProtocolError,
    NegotiationError,
    NegotiationFailed,
    NegotiationContradiction,
    NegotiationTampered,
    ExtensionNotNegotiated,
    ConnectionClosed,
)
from .extensions import Extension, ExtensionRegistry, BUILTIN_EXTENSIONS, registry
from .handshake import negotiate, validate_offer
from .session import Session, MemoryPipe

__all__ = [
    "ProtocolError",
    "NegotiationError",
    "NegotiationFailed",
    "NegotiationContradiction",
    "NegotiationTampered",
    "ExtensionNotNegotiated",
    "ConnectionClosed",
    "Extension",
    "ExtensionRegistry",
    "BUILTIN_EXTENSIONS",
    "registry",
    "negotiate",
    "validate_offer",
    "Session",
    "MemoryPipe",
]
