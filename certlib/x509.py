"""X.509 证书解析：主体、签发者、有效期、用途、公钥参数、签名。

结构（RFC 5280）：
  Certificate ::= SEQUENCE {
    tbsCertificate       TBSCertificate,
    signatureAlgorithm   AlgorithmIdentifier,
    signatureValue       BIT STRING }
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import asn1
from .errors import (
    CertError,
    EncodingError,
    FailureCode,
    StructureError,
)

# ---- 相关 OID ----
OID_SHA256_WITH_RSA = "1.2.840.113549.1.1.11"
OID_SHA1_WITH_RSA = "1.2.840.113549.1.1.5"
OID_RSA_ENCRYPTION = "1.2.840.113549.1.1.1"

OID_BASIC_CONSTRAINTS = "2.5.29.19"
OID_KEY_USAGE = "2.5.29.15"
OID_EXT_KEY_USAGE = "2.5.29.37"

OID_SERVER_AUTH = "1.3.6.1.5.5.7.3.1"
OID_CLIENT_AUTH = "1.3.6.1.5.5.7.3.2"
OID_CODE_SIGNING = "1.3.6.1.5.5.7.3.3"
OID_EMAIL_PROTECTION = "1.3.6.1.5.5.7.3.4"
OID_ANY_EKU = "2.5.29.37.0"

ATTRIBUTE_SHORT_NAMES = {
    "2.5.4.3": "CN",
    "2.5.4.6": "C",
    "2.5.4.7": "L",
    "2.5.4.8": "ST",
    "2.5.4.10": "O",
    "2.5.4.11": "OU",
    "1.2.840.113549.1.9.1": "emailAddress",
}

KEY_USAGE_BITS = [
    "digitalSignature", "nonRepudiation", "keyEncipherment", "dataEncipherment",
    "keyAgreement", "keyCertSign", "cRLSign", "encipherOnly", "decipherOnly",
]

EKU_NAMES = {
    OID_SERVER_AUTH: "serverAuth",
    OID_CLIENT_AUTH: "clientAuth",
    OID_CODE_SIGNING: "codeSigning",
    OID_EMAIL_PROTECTION: "emailProtection",
    OID_ANY_EKU: "anyExtendedKeyUsage",
}

HASH_BY_SIG_OID = {
    OID_SHA256_WITH_RSA: hashlib.sha256,
    OID_SHA1_WITH_RSA: hashlib.sha1,
}


def parse_time(node: asn1.Node) -> datetime:
    """解析 UTCTime / GeneralizedTime 为带时区的 datetime。"""
    if node.tag == asn1.TAG_UTC_TIME:
        text = node.value.decode("ascii", errors="strict") if _is_ascii(node.value) else None
        if text is None:
            raise EncodingError(FailureCode.INVALID_TIME, "UTCTime 非 ASCII")
        fmt, year_base = "%y%m%d%H%M%SZ", None
    elif node.tag == asn1.TAG_GENERALIZED_TIME:
        if not _is_ascii(node.value):
            raise EncodingError(FailureCode.INVALID_TIME, "GeneralizedTime 非 ASCII")
        text = node.value.decode("ascii")
        fmt, year_base = "%Y%m%d%H%M%SZ", None
    else:
        raise EncodingError(
            FailureCode.UNEXPECTED_TAG,
            f"偏移 {node.start}: 期望时间类型，实际 tag 0x{node.tag:02X}",
        )
    try:
        dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise EncodingError(
            FailureCode.INVALID_TIME, f"时间 {text!r} 格式非法: {exc}"
        ) from exc
    return dt


def _is_ascii(raw: bytes) -> bool:
    return all(b < 0x80 for b in raw)


@dataclass(frozen=True)
class Name:
    """X.501 Name：有序的 (oid, value) 列表。"""

    rdns: tuple[tuple[tuple[str, str], ...], ...]

    def to_dict(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for rdn in self.rdns:
            for oid_, value in rdn:
                out[ATTRIBUTE_SHORT_NAMES.get(oid_, oid_)] = value
        return out

    def __str__(self) -> str:
        parts = []
        for rdn in self.rdns:
            parts.append("+".join(
                f"{ATTRIBUTE_SHORT_NAMES.get(o, o)}={v}" for o, v in rdn
            ))
        return ", ".join(parts)


def parse_name(node: asn1.Node, what: str) -> Name:
    node.expect(0x30, what)
    rdns = []
    for rdn in node.child_values():
        rdn.expect(0x31, f"{what}/RDN")
        attrs = []
        for atv in rdn.child_values():
            atv.expect(0x30, f"{what}/AttributeTypeAndValue")
            pair = atv.child_values()
            if len(pair) != 2:
                raise StructureError(
                    FailureCode.MISSING_FIELD,
                    f"{what}: AttributeTypeAndValue 应有 2 个元素，实际 {len(pair)}",
                )
            oid_ = asn1.decode_oid(pair[0], f"{what}/attributeType")
            value = asn1.decode_string(pair[1], f"{what}/attributeValue")
            attrs.append((oid_, value))
        if not attrs:
            raise StructureError(
                FailureCode.MISSING_FIELD, f"{what}: 存在空的 RDN"
            )
        rdns.append(tuple(attrs))
    if not rdns:
        raise StructureError(FailureCode.MISSING_FIELD, f"{what}: 名称为空")
    return Name(tuple(rdns))


@dataclass(frozen=True)
class PublicKeyInfo:
    algorithm_oid: str
    # RSA 参数；其它算法为 None。
    modulus: int | None
    exponent: int | None
    raw_bitstring: bytes

    def to_dict(self) -> dict:
        return {
            "algorithm": self.algorithm_oid,
            "modulus_bits": self.modulus.bit_length() if self.modulus else None,
            "exponent": self.exponent,
        }


def parse_spki(node: asn1.Node) -> PublicKeyInfo:
    node.expect(0x30, "subjectPublicKeyInfo")
    parts = node.child_values()
    if len(parts) != 2:
        raise StructureError(
            FailureCode.MISSING_FIELD,
            f"subjectPublicKeyInfo 应有 2 个元素，实际 {len(parts)}",
        )
    alg = parts[0].expect(0x30, "algorithm").child_values()
    if not alg:
        raise StructureError(FailureCode.MISSING_FIELD, "algorithm 为空")
    alg_oid = asn1.decode_oid(alg[0], "algorithm OID")
    key_bits = asn1.decode_bitstring(parts[1], "subjectPublicKey")
    modulus = exponent = None
    if alg_oid == OID_RSA_ENCRYPTION:
        rsa_node = asn1.decode_exact(key_bits)
        rsa_node.expect(0x30, "RSAPublicKey")
        rsa_parts = rsa_node.child_values()
        if len(rsa_parts) != 2:
            raise StructureError(
                FailureCode.MISSING_FIELD, "RSAPublicKey 应有 modulus 与 exponent"
            )
        modulus = asn1.decode_integer(rsa_parts[0], "modulus")
        exponent = asn1.decode_integer(rsa_parts[1], "exponent")
        if modulus <= 0 or exponent <= 0:
            raise StructureError(
                FailureCode.MISSING_FIELD, "RSA 公钥参数必须为正整数"
            )
    return PublicKeyInfo(alg_oid, modulus, exponent, key_bits)


@dataclass(frozen=True)
class Extensions:
    key_usage: frozenset[str] | None = None
    key_usage_critical: bool = False
    basic_constraints_ca: bool | None = None
    path_len: int | None = None
    extended_key_usage: frozenset[str] | None = None

    def to_dict(self) -> dict:
        return {
            "keyUsage": sorted(self.key_usage) if self.key_usage is not None else None,
            "basicConstraintsCA": self.basic_constraints_ca,
            "pathLenConstraint": self.path_len,
            "extendedKeyUsage": (
                sorted(self.extended_key_usage)
                if self.extended_key_usage is not None else None
            ),
        }


def parse_extensions(node: asn1.Node) -> Extensions:
    node.expect(0x30, "Extensions")
    key_usage = None
    key_usage_critical = False
    bc_ca = None
    path_len = None
    eku = None
    for ext in node.child_values():
        ext.expect(0x30, "Extension")
        items = ext.child_values()
        if len(items) < 2:
            raise StructureError(
                FailureCode.MISSING_FIELD, "Extension 缺少 extnID 或 extnValue"
            )
        ext_oid = asn1.decode_oid(items[0], "extnID")
        idx = 1
        critical = False
        if items[idx].tag == asn1.TAG_BOOLEAN:
            critical = asn1.decode_boolean(items[idx], "critical")
            idx += 1
        if idx >= len(items) or items[idx].tag != asn1.TAG_OCTET_STRING:
            raise StructureError(
                FailureCode.MISSING_FIELD, f"扩展 {ext_oid} 缺少 extnValue"
            )
        payload = asn1.decode_exact(items[idx].value)

        if ext_oid == OID_KEY_USAGE:
            bits = asn1.decode_bitstring(payload, "KeyUsage")
            key_usage = frozenset(
                name for i, name in enumerate(KEY_USAGE_BITS)
                if i // 8 < len(bits) and bits[i // 8] & (0x80 >> (i % 8))
            )
            key_usage_critical = critical
        elif ext_oid == OID_BASIC_CONSTRAINTS:
            bc_items = payload.expect(0x30, "BasicConstraints").child_values()
            bc_ca = False
            for item in bc_items:
                if item.tag == asn1.TAG_BOOLEAN:
                    bc_ca = asn1.decode_boolean(item, "cA")
                elif item.tag == asn1.TAG_INTEGER:
                    path_len = asn1.decode_integer(item, "pathLenConstraint")
        elif ext_oid == OID_EXT_KEY_USAGE:
            eku_seq = payload.expect(0x30, "ExtKeyUsageSyntax").child_values()
            if not eku_seq:
                raise StructureError(
                    FailureCode.MISSING_FIELD, "extendedKeyUsage 不允许为空序列"
                )
            eku = frozenset(
                asn1.decode_oid(e, "EKU OID") for e in eku_seq
            )
    return Extensions(key_usage, key_usage_critical, bc_ca, path_len, eku)


@dataclass
class Certificate:
    subject: Name
    issuer: Name
    not_before: datetime
    not_after: datetime
    serial_number: int
    signature_algorithm_oid: str
    public_key: PublicKeyInfo
    extensions: Extensions
    signature: bytes
    tbs_der: bytes
    raw_der: bytes

    def to_dict(self) -> dict:
        return {
            "subject": self.subject.to_dict(),
            "issuer": self.issuer.to_dict(),
            "serialNumber": f"{self.serial_number:X}",
            "notBefore": self.not_before.strftime("%b %d %H:%M:%S %Y GMT"),
            "notAfter": self.not_after.strftime("%b %d %H:%M:%S %Y GMT"),
            "signatureAlgorithm": self.signature_algorithm_oid,
            "publicKey": self.public_key.to_dict(),
            "extensions": self.extensions.to_dict(),
        }


def _explicit_context(tag_number: int) -> int:
    return 0xA0 | tag_number


def parse_certificate(der: bytes) -> Certificate:
    """解析 DER 编码的 X.509 证书。"""
    root = asn1.decode_exact(der)
    root.expect(0x30, "Certificate")
    top = root.child_values()
    if len(top) != 3:
        raise StructureError(
            FailureCode.MISSING_FIELD,
            f"Certificate 应有 3 个元素，实际 {len(top)}",
        )
    tbs, sig_alg_node, sig_value_node = top

    tbs.expect(0x30, "TBSCertificate")
    tbs_der = der[tbs.start:tbs.end]
    items = list(tbs.child_values())
    pos = 0

    # [0] EXPLICIT version，可选，缺省 v1。
    version = 1
    if pos < len(items) and items[pos].tag == _explicit_context(0):
        inner = items[pos].child_values()
        if len(inner) != 1:
            raise StructureError(FailureCode.MISSING_FIELD, "version 包装为空")
        version = asn1.decode_integer(inner[0], "version") + 1
        pos += 1
    if version not in (1, 2, 3):
        raise StructureError(
            FailureCode.UNSUPPORTED_VERSION, f"不支持的证书版本 v{version}"
        )

    def need(what: str) -> asn1.Node:
        nonlocal pos
        if pos >= len(items):
            raise StructureError(
                FailureCode.MISSING_FIELD, f"TBSCertificate 缺少必需字段 {what}"
            )
        node = items[pos]
        pos += 1
        return node

    serial = asn1.decode_integer(need("serialNumber"), "serialNumber")
    if serial < 0:
        raise StructureError(
            FailureCode.MISSING_FIELD, "serialNumber 不允许为负数"
        )

    tbs_sig_alg = need("signature").expect(0x30, "signature").child_values()
    if not tbs_sig_alg:
        raise StructureError(FailureCode.MISSING_FIELD, "signature 算法为空")
    tbs_sig_oid = asn1.decode_oid(tbs_sig_alg[0], "signature OID")

    issuer = parse_name(need("issuer"), "issuer")

    validity = need("validity").expect(0x30, "validity").child_values()
    if len(validity) != 2:
        raise StructureError(
            FailureCode.MISSING_FIELD, "validity 应有 notBefore/notAfter 两项"
        )
    not_before = parse_time(validity[0])
    not_after = parse_time(validity[1])

    subject = parse_name(need("subject"), "subject")
    public_key = parse_spki(need("subjectPublicKeyInfo"))

    # 可选的 [1]/[2]（issuer/subject UniqueID）与 [3] extensions。
    extensions = Extensions()
    while pos < len(items):
        node = items[pos]
        pos += 1
        if node.tag == _explicit_context(3):
            inner = node.child_values()
            if len(inner) != 1:
                raise StructureError(
                    FailureCode.MISSING_FIELD, "extensions 包装为空"
                )
            extensions = parse_extensions(inner[0])
        elif node.tag in (_explicit_context(1), _explicit_context(2)):
            continue  # UniqueID，解析但当前不使用
        else:
            raise EncodingError(
                FailureCode.UNEXPECTED_TAG,
                f"偏移 {node.start}: TBSCertificate 出现未知字段 tag 0x{node.tag:02X}",
            )

    outer_alg = sig_alg_node.expect(0x30, "signatureAlgorithm").child_values()
    if not outer_alg:
        raise StructureError(FailureCode.MISSING_FIELD, "signatureAlgorithm 为空")
    outer_sig_oid = asn1.decode_oid(outer_alg[0], "signatureAlgorithm OID")
    if outer_sig_oid != tbs_sig_oid:
        raise StructureError(
            FailureCode.MISSING_FIELD,
            f"内外签名算法不一致: {tbs_sig_oid} vs {outer_sig_oid}",
        )

    signature = asn1.decode_bitstring(sig_value_node, "signatureValue")

    return Certificate(
        subject=subject,
        issuer=issuer,
        not_before=not_before,
        not_after=not_after,
        serial_number=serial,
        signature_algorithm_oid=outer_sig_oid,
        public_key=public_key,
        extensions=extensions,
        signature=signature,
        tbs_der=tbs_der,
        raw_der=der,
    )


def verify_rsa_signature(cert: Certificate, issuer_key: PublicKeyInfo) -> bool:
    """纯 Python RSA PKCS#1 v1.5 验签（支持 sha256/sha1WithRSAEncryption）。"""
    hash_fn = HASH_BY_SIG_OID.get(cert.signature_algorithm_oid)
    if hash_fn is None:
        raise StructureError(
            FailureCode.UNSUPPORTED_ALGORITHM,
            f"不支持的签名算法 {cert.signature_algorithm_oid}",
        )
    if issuer_key.modulus is None or issuer_key.exponent is None:
        raise StructureError(
            FailureCode.UNSUPPORTED_ALGORITHM,
            f"签发者公钥算法 {issuer_key.algorithm_oid} 不是 RSA",
        )
    n, e = issuer_key.modulus, issuer_key.exponent
    k = (n.bit_length() + 7) // 8
    if len(cert.signature) != k:
        return False
    s = int.from_bytes(cert.signature, "big")
    if s >= n:
        return False
    m = pow(s, e, n)
    em = m.to_bytes(k, "big")
    digest = hash_fn(cert.tbs_der).digest()
    digest_info_prefix = {
        "sha256": bytes.fromhex("3031300d060960864801650304020105000420"),
        "sha1": bytes.fromhex("3021300906052b0e03021a05000414"),
    }[hash_fn().name]
    expected = (
        b"\x00\x01" + b"\xff" * (k - len(digest_info_prefix) - len(digest) - 3)
        + b"\x00" + digest_info_prefix + digest
    )
    return em == expected
