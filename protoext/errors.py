"""协议错误类型。

所有协商相关错误都携带双方能力清单（local_caps / remote_caps），
便于上层日志与运维定位版本错配问题。
"""


class ProtocolError(Exception):
    """协议层错误基类。"""


class MalformedMessageError(ProtocolError):
    """帧无法解析（非 JSON、非对象、超长等）。"""


class FrameTooLargeError(MalformedMessageError):
    """帧长度超过上限。"""


class UnexpectedMessageError(ProtocolError):
    """当前阶段收到了意料之外的消息类型。"""


class NegotiationError(ProtocolError):
    """协商类错误基类，携带双方能力清单。"""

    def __init__(self, message, local_caps=(), remote_caps=()):
        self.local_caps = tuple(sorted(local_caps))
        self.remote_caps = tuple(sorted(remote_caps))
        super().__init__(message)

    def __str__(self):
        return (
            f"{super().__str__()} "
            f"[local_caps={list(self.local_caps)} "
            f"remote_caps={list(self.remote_caps)}]"
        )


class NegotiationFailedError(NegotiationError):
    """对端缺少我方必需的扩展，协商失败。"""

    def __init__(self, message, local_caps=(), remote_caps=(), missing=()):
        self.missing = tuple(sorted(missing))
        super().__init__(message, local_caps, remote_caps)

    def __str__(self):
        return f"{super().__str__()} missing_required={list(self.missing)}"


class ContradictionError(NegotiationError):
    """对端声明自相矛盾（如接受了从未提供的扩展）。"""


class TamperDetectedError(ProtocolError):
    """握手转录校验失败：协商字段疑似被中间人篡改。"""


class ExtensionNotNegotiatedError(ProtocolError):
    """试图发送未协商成功的扩展字段（发送侧主动拒绝）。"""


class UnnegotiatedExtensionError(ProtocolError):
    """收到了未协商成功的扩展字段（接收侧拒绝）。"""


class UnknownFieldError(ProtocolError):
    """收到既不属于基础协议也不属于任何已知扩展的字段。"""
