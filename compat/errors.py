"""读取拒绝的统一原因码与异常。

实现方（被测的读取器）只允许两种行为：
1. 返回 framework.ReadOk（读取成功）；
2. 抛出 RejectError（明确拒绝，必须带原因码）。
除此之外（返回错误内容、返回部分内容、抛其它异常、崩溃）一律判失败。
"""


class Reason:
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"  # 文件版本超出本实现支持范围
    KEY_UNAVAILABLE = "KEY_UNAVAILABLE"          # 文件要求的密钥版本不在密钥环中
    INTEGRITY = "INTEGRITY"                      # 块校验失败（被篡改或损坏）
    MALFORMED = "MALFORMED"                      # 文件结构不合法


class RejectError(Exception):
    """读取器明确拒绝文件时抛出，必须携带原因码。"""

    def __init__(self, reason, detail=""):
        super().__init__("%s: %s" % (reason, detail))
        self.reason = reason
        self.detail = detail
