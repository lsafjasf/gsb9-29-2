"""协议错误类型。

所有协商类错误都携带双方能力清单（offered / required / supported / agreed），
便于对端与运维定位分歧。
"""


class ProtocolError(Exception):
    """线路格式、帧结构或协议状态机错误。"""


class NegotiationError(ProtocolError):
    """扩展协商相关错误的基类，携带双方能力清单。"""

    def __init__(self, message, *, offered=(), required=(), supported=(), agreed=(),
                 used=None, peer_capabilities=None):
        super().__init__(message)
        self.offered = sorted(offered)
        self.required = sorted(required)
        self.supported = sorted(supported)
        self.agreed = sorted(agreed)
        self.used = sorted(used) if used else []
        self.peer_capabilities = peer_capabilities

    def capability_report(self):
        lines = [
            f"  client offered  : {self.offered}",
            f"  client required : {self.required}",
            f"  server supported: {self.supported}",
            f"  agreed          : {self.agreed}",
        ]
        if self.used:
            lines.append(f"  peer used       : {self.used}")
        if self.peer_capabilities is not None:
            lines.append(f"  peer raw view   : {self.peer_capabilities}")
        return "\n".join(lines)

    def __str__(self):
        return f"{super().__str__()}\n能力清单:\n{self.capability_report()}"


class NegotiationFailed(NegotiationError):
    """客户端要求的扩展无法与服务端能力取交集，协商失败。"""


class NegotiationContradiction(NegotiationError):
    """对端声明自相矛盾。

    例如：ack 中包含客户端从未提供的扩展；数据帧使用了未协商的扩展；
    协商字段类型非法等。
    """


class NegotiationTampered(NegotiationError):
    """握手指纹不一致：协商字段在传输中被篡改或被剥离降级。"""


class ExtensionNotNegotiated(NegotiationError):
    """本端代码尝试使用一个未协商成功的扩展（保证“完全不可见”）。"""


class ConnectionClosed(ProtocolError):
    """对端正常关闭（fin）或连接在数据阶段结束。"""
