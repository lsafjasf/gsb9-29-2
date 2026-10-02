"""证书解析与校验的失败分类。

所有失败都携带 machine-readable 的 code，便于调用方区分原因。
"""

from enum import Enum


class FailureCode(Enum):
    # ---- 编码层（DER）----
    TRUNCATED = "TRUNCATED"                    # 数据被截断，TLV 不完整
    INDEFINITE_LENGTH = "INDEFINITE_LENGTH"    # DER 不允许不定长编码
    LENGTH_OVERFLOW = "LENGTH_OVERFLOW"        # 长度字段本身超长/越界
    NON_MINIMAL_LENGTH = "NON_MINIMAL_LENGTH"  # 长格式长度未用最短编码
    NON_MINIMAL_INTEGER = "NON_MINIMAL_INTEGER"  # INTEGER 有冗余前导字节
    FIELD_TOO_LONG = "FIELD_TOO_LONG"          # 字段长度超过实现上限
    UNEXPECTED_TAG = "UNEXPECTED_TAG"          # 必需字段的 tag 不符
    TRAILING_DATA = "TRAILING_DATA"            # 结构结束后还有多余字节
    INVALID_TIME = "INVALID_TIME"              # 时间字段格式非法
    INVALID_BITSTRING = "INVALID_BITSTRING"    # BIT STRING 编码非法
    INVALID_OID = "INVALID_OID"                # OID 编码非法
    INVALID_UTF8 = "INVALID_UTF8"              # 字符串解码失败

    # ---- 结构层（X.509）----
    MISSING_FIELD = "MISSING_FIELD"            # 必需字段缺失
    UNSUPPORTED_ALGORITHM = "UNSUPPORTED_ALGORITHM"  # 不支持的签名/密钥算法
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"

    # ---- 校验层 ----
    NOT_YET_VALID = "NOT_YET_VALID"            # 当前时间早于 notBefore
    EXPIRED = "EXPIRED"                        # 当前时间晚于 notAfter
    USAGE_MISMATCH = "USAGE_MISMATCH"          # keyUsage/EKU 不满足用途
    NOT_A_CA = "NOT_A_CA"                      # 签发者缺少 CA 基本约束
    ISSUER_MISMATCH = "ISSUER_MISMATCH"        # 链上 subject/issuer 名称不匹配
    SIGNATURE_INVALID = "SIGNATURE_INVALID"    # 签名校验失败
    PATH_LENGTH_EXCEEDED = "PATH_LENGTH_EXCEEDED"  # 超过 basicConstraints pathLen


class CertError(Exception):
    """所有证书相关失败的基类，code 区分失败类别。"""

    def __init__(self, code: FailureCode, message: str):
        self.code = code
        super().__init__(f"[{code.value}] {message}")


class EncodingError(CertError):
    """DER 编码层错误（畸形编码、超长字段等）。"""


class StructureError(CertError):
    """X.509 结构层错误（缺失必需字段、不支持的算法等）。"""


class ValidationError(CertError):
    """校验层错误（有效期、用途、链式关系等）。"""
