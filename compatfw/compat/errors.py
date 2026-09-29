"""参考加密容器格式的错误类型。

所有“明确拒绝”都必须以 EncFormatError 的某个子类抛出，并携带
机器可读的原因码（reason），框架据此给出 REJECT 判定与依据。
"""


class EncFormatError(Exception):
    """所有可预期的格式/解密失败基类。"""

    reason = "enc_format_error"

    def __init__(self, message, reason=None):
        super().__init__(message)
        if reason is not None:
            self.reason = reason


class UnsupportedVersionError(EncFormatError):
    """读取方不支持该文件版本（如 v1 程序读到 v2 文件）。"""

    reason = "unsupported_version"


class KeyNotFoundError(EncFormatError):
    """密钥环中没有文件头声明的 key_version 对应密钥。"""

    reason = "key_not_found"


class UnsupportedBlockSizeError(EncFormatError):
    """块大小不在读取方支持范围内。"""

    reason = "unsupported_block_size"


class IntegrityError(EncFormatError):
    """结构完整但鉴权失败：头 HMAC、块 HMAC、截断/尾部多余字节等。"""

    reason = "integrity_error"


class DecryptError(EncFormatError):
    """解密过程本身失败（结构损坏到无法解析等）。"""

    reason = "decrypt_error"
