"""证书解析与校验的错误分类。

所有错误都带 ``code``（机器可读）与 ``message``（人可读原因），
校验类 API 不抛异常而是返回错误对象列表，便于一次性报告全部问题。
"""


class CertError(Exception):
    """所有证书相关错误的基类。"""

    code = "cert.error"

    def __init__(self, message, code=None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code

    def to_dict(self):
        return {"code": self.code, "message": self.message}

    def __repr__(self):
        return "%s(%s: %s)" % (type(self).__name__, self.code, self.message)


class DerError(CertError):
    """DER/TLV 编码层错误（畸形编码、超长字段、截断等）。"""

    code = "der.error"


class StructureError(CertError):
    """证书结构层错误（缺失必需字段、字段类型不符等）。"""

    code = "cert.structure"


class ValidityError(CertError):
    """有效期校验失败（尚未生效 / 已过期）。"""

    code = "validity.error"


class PurposeError(CertError):
    """用途校验失败（keyUsage / extendedKeyUsage 不匹配）。"""

    code = "purpose.error"


class ChainError(CertError):
    """链式签发关系校验失败（签发者不符、签名无效、非 CA 等）。"""

    code = "chain.error"
