"""difftest：协议解析器差分测试框架（仅使用 Python 标准库）。

约定：被比较的两个解析器签名均为 ``parse(data: bytes) -> object``，
解析成功返回 JSON 可序列化的结果，解析失败抛出异常，
异常的类名会被当作“错误类别”参与比较。
"""
from .harness import (
    MATCH,
    VALUE_MISMATCH,
    OUTCOME_MISMATCH,
    ERROR_MISMATCH,
    Outcome,
    classify,
    run_parser,
)
from .campaign import Campaign, DiffConfig

__all__ = [
    "MATCH",
    "VALUE_MISMATCH",
    "OUTCOME_MISMATCH",
    "ERROR_MISMATCH",
    "Outcome",
    "classify",
    "run_parser",
    "Campaign",
    "DiffConfig",
]

__version__ = "0.1.0"
