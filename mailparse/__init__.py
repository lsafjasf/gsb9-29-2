"""MIME 邮件报文解析库（仅使用 Python 3 标准库）。"""

from .parser import Part, parse_message, parse_file

__all__ = ["Part", "parse_message", "parse_file"]
