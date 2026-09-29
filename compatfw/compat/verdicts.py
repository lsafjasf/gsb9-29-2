"""兼容性判定结论（三值通过判据 + 辅助状态）。

通过判据（只认以下三种，且依据必须充分）：

EQUIV    可读且内容与源明文逐字节等价（哈希+长度双重比对），
         且读取方原生支持该格式版本，无需转换。=> 通过
CONVERT  可读且内容逐字节等价，但读取方原生版本比文件新，
         明确报告 needs_conversion（需要用新版本重写）。=> 通过（带转换动作）
REJECT   明确拒绝：抛出带原因码的 EncFormatError，未返回任何部分明文。
         => 通过（拒绝类用例的期望结果）

以下为失败/待定状态：
PARTIAL  读到部分内容（返回截断明文或先产出数据后报错）。明确判为不通过。
FAIL     发生了与期望不符的行为：返回内容不等价、抛出未预期的异常类型、
         或期望 REJECT 却解密成功等。=> 不通过
ERROR    框架/适配器自身出错（非被测格式异常）。=> 不通过
NA       该组合不适用（例如“缺失密钥”变体对“写入器”没有意义）。
"""

from enum import Enum


class Verdict(str, Enum):
    EQUIV = "EQUIV"
    CONVERT = "CONVERT"
    REJECT = "REJECT"
    PARTIAL = "PARTIAL"
    FAIL = "FAIL"
    ERROR = "ERROR"
    NA = "NA"

    @property
    def passed(self) -> bool:
        return self in (Verdict.EQUIV, Verdict.CONVERT, Verdict.REJECT)


# REJECT 原因码 -> 人类可读说明
REJECT_REASONS = {
    "unsupported_version": "读取方不支持文件格式版本（旧程序读新文件）",
    "key_not_found": "密钥环缺少文件头声明的 key_version",
    "unsupported_block_size": "块大小超出读取方支持范围",
    "header_mac_mismatch": "文件头 HMAC 校验失败（头被篡改或密钥不匹配）",
    "block_mac_mismatch": "数据块 HMAC 校验失败（块被篡改或密钥不匹配）",
    "truncated": "文件/块截断，声明的块未完整出现",
    "trailing_data": "声明块之后存在多余字节",
    "block_index_mismatch": "块序号不连续",
    "block_too_large": "块长度超过声明的块大小",
    "bad_magic": "文件魔数错误，不是本格式文件",
    "unknown_version": "出现未知格式版本号",
    "enc_format_error": "其他明确的格式错误",
}
