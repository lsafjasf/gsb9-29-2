# 失败分类说明

所有失败都继承 `certlib.errors.CertError`，携带枚举 `FailureCode`，
调用方可按 `exc.code` 精确区分原因。链式校验不短路，`ValidationReport`
会收集链上全部问题（见 `Issue`：`code + 证书主体 + 人话原因`）。

## 编码层（`EncodingError`）

| code | 触发条件 |
|---|---|
| `TRUNCATED` | 数据截断：缺 tag/长度字节、长度字段后续字节不足、V 部分比声明短、构造类型子元素未填满 |
| `INDEFINITE_LENGTH` | 长度首字节为 `0x80`（不定长），DER 禁止 |
| `LENGTH_OVERFLOW` | 长格式长度的后续字节数 > 8，无法在整数内容纳 |
| `NON_MINIMAL_LENGTH` | 长度未用最短编码：前导 `0x00`，或本可用短格式却用长格式 |
| `NON_MINIMAL_INTEGER` | INTEGER 冗余前导字节（正数多余 `0x00`、负数多余 `0xFF`），或 BOOLEAN 非 `0x00/0xFF` |
| `FIELD_TOO_LONG` | 字段声明长度超过实现上限 `MAX_FIELD_LENGTH`（16 MiB） |
| `UNEXPECTED_TAG` | 必需位置 tag 不符；多字节高位号 tag；非字符串 tag 按字符串解码 |
| `TRAILING_DATA` | 顶层 TLV 结束后仍有多余字节 |
| `INVALID_TIME` | UTCTime/GeneralizedTime 不是合法的 `%y%m%d%H%M%SZ` / `%Y%m%d%H%M%SZ` |
| `INVALID_BITSTRING` | 缺未用位计数、未用位数 > 7、未用位非零 |
| `INVALID_OID` | OID 为空、分量非最小编码（前导 `0x80`）、末分量未结束 |
| `INVALID_UTF8` | UTF8String 非合法 UTF-8，或 PrintableString/IA5String 非 ASCII |

## 结构层（`StructureError`）

| code | 触发条件 |
|---|---|
| `MISSING_FIELD` | 必需字段缺失（如 Certificate 非 3 元素、缺 validity/subject/SPKI、扩展缺 extnValue、名称为空、空链） |
| `UNSUPPORTED_ALGORITHM` | 签名/公钥算法不在支持范围（目前支持 RSA + sha1/sha256 PKCS#1 v1.5） |
| `UNSUPPORTED_VERSION` | version 不是 v1/v2/v3 |

## 校验层（`ValidationError`）

| code | 触发条件 |
|---|---|
| `NOT_YET_VALID` | 当前时间 < `notBefore` |
| `EXPIRED` | 当前时间 > `notAfter` |
| `USAGE_MISMATCH` | 末端证书 keyUsage 位不全，或 EKU 不含所需用途且不含 `anyExtendedKeyUsage`，或没有 EKU 扩展 |
| `NOT_A_CA` | 作为签发者的证书 `basicConstraints.cA` 非 TRUE |
| `ISSUER_MISMATCH` | 子证书 issuer Name 与签发者 subject Name 不等；或链末信任锚非自签名 |
| `SIGNATURE_INVALID` | RSA PKCS#1 v1.5 验签失败：签名长度 ≠ 模数字节数、`s ≥ n`、EM 不匹配；自签名根自验失败 |
| `PATH_LENGTH_EXCEEDED` | 某 CA 下方非自签中间 CA 数量超过其 `basicConstraints.pathLenConstraint` |

时间边界：`now == notBefore` / `now == notAfter` 视为有效（半开区间），
`now > notAfter` 才判 `EXPIRED`。
