"""加密文件格式跨版本兼容性测试框架。

包含一个最小但完整的参考加密容器格式（v1/v2），以及对其跨版本
读写组合进行系统验证的兼容性矩阵框架。仅使用 Python 3 标准库。

注意：参考格式中的流加密仅用于演示兼容性测试方法，不可用于生产。
"""

from .errors import (
    DecryptError,
    EncFormatError,
    IntegrityError,
    KeyNotFoundError,
    UnsupportedBlockSizeError,
    UnsupportedVersionError,
)
from .codec import Reader, Writer, KeyRing, ReadResult, MAGIC, V1, V2

__all__ = [
    "DecryptError",
    "EncFormatError",
    "IntegrityError",
    "KeyNotFoundError",
    "UnsupportedBlockSizeError",
    "UnsupportedVersionError",
    "Reader",
    "Writer",
    "KeyRing",
    "ReadResult",
    "MAGIC",
    "V1",
    "V2",
]
