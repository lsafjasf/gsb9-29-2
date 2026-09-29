"""解析错误类别。

被测的两个实现必须抛出 ParseError（或任何带 .category 属性的异常），
框架按 category 归类错误，从而区分"两边都失败但错误类别不同"。
"""


class Category:
    TRUNCATED_HEADER = "TRUNCATED_HEADER"      # 剩余字节不足一个记录头
    TRUNCATED_PAYLOAD = "TRUNCATED_PAYLOAD"    # 声明长度超过剩余字节
    UNKNOWN_TYPE = "UNKNOWN_TYPE"              # 未知记录类型
    DEPTH_EXCEEDED = "DEPTH_EXCEEDED"          # 嵌套深度超限
    UINT_BAD_LENGTH = "UINT_BAD_LENGTH"        # UINT 载荷长度不在 1..=8
    INVALID_UTF8 = "INVALID_UTF8"              # STR 载荷不是合法 UTF-8

    ALL = frozenset({
        TRUNCATED_HEADER, TRUNCATED_PAYLOAD, UNKNOWN_TYPE,
        DEPTH_EXCEEDED, UINT_BAD_LENGTH, INVALID_UTF8,
    })


class ParseError(Exception):
    """协议解析错误，category 必须是 Category.ALL 之一。"""

    def __init__(self, category, message="", offset=None):
        if category not in Category.ALL:
            raise ValueError(f"unknown error category: {category!r}")
        super().__init__(message or category)
        self.category = category
        self.offset = offset
