"""protoext —— 带扩展协商的长度前缀帧协议库（仅标准库）。

用法::

    server = Session(transport, "server", extensions=["compression"])
    client = Session(transport, "client", extensions=["compression", "priority"])
    server.handshake(); client.handshake()   # 两端各自驱动（可跨线程）
    client.send_data("hello", ext={"priority": {"prio": 5}})
"""

from .errors import (
    ContradictionError,
    ExtensionNotNegotiatedError,
    FrameTooLargeError,
    MalformedMessageError,
    NegotiationError,
    NegotiationFailedError,
    ProtocolError,
    TamperDetectedError,
    UnexpectedMessageError,
    UnknownFieldError,
    UnnegotiatedExtensionError,
)
from .registry import EXTENSIONS
from .session import Session
from .codec import SocketTransport

__all__ = [
    "Session",
    "SocketTransport",
    "EXTENSIONS",
    "ProtocolError",
    "MalformedMessageError",
    "FrameTooLargeError",
    "UnexpectedMessageError",
    "NegotiationError",
    "NegotiationFailedError",
    "ContradictionError",
    "TamperDetectedError",
    "ExtensionNotNegotiatedError",
    "UnnegotiatedExtensionError",
    "UnknownFieldError",
]

__version__ = "1.0.0"
