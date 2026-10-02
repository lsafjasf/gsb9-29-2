"""X.509 证书（RFC 5280）结构解析。

输入 DER 字节，输出主体、签发者、有效期、用途（keyUsage/EKU）、
公钥参数等字段。所有字段按变长 TLV 解析，不使用固定偏移。
"""

import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import der
from .der import (
    TAG_BIT_STRING, TAG_INTEGER, TAG_OCTET_STRING, TAG_OID, TAG_SEQUENCE,
    TAG_SET, TLV, parse_bit_string, parse_integer, parse_oid,
)
from .errors import DerError, StructureError
from .oids import EKU_OID_TO_NAME, name_of
from .timeutil import parse_time

KEY_USAGE_BITS = [
    "digitalSignature", "nonRepudiation", "keyEncipherment",
    "dataEncipherment", "keyAgreement", "keyCertSign",
    "cRLSign", "encipherOnly", "decipherOnly",
]


def _require(cond, message, code="cert.missing_field"):
    if not cond:
        raise StructureError(message, code=code)


def parse_name(tlv: TLV) -> List[Tuple[str, str]]:
    """RDNSequence -> [(OID名, 值), ...]，保持证书中的顺序。"""
    tlv.expect(TAG_SEQUENCE)
    result = []
    for rdn in tlv.children:
        rdn.expect(TAG_SET)
        for atv in rdn.children:
            atv.expect(TAG_SEQUENCE)
            _require(len(atv.children) == 2,
                     "AttributeTypeAndValue 应有 2 个子元素")
            oid = parse_oid(atv.child(0, TAG_OID))
            value = der.decode_any_string(atv.children[1])
            result.append((name_of(oid), value))
    return result


def format_name(pairs: List[Tuple[str, str]]) -> str:
    return ", ".join("%s=%s" % kv for kv in pairs)


def _parse_algorithm_identifier(tlv: TLV) -> Tuple[str, Optional[TLV]]:
    tlv.expect(TAG_SEQUENCE)
    _require(len(tlv.children) >= 1, "AlgorithmIdentifier 缺少算法 OID")
    oid = parse_oid(tlv.child(0, TAG_OID))
    params = tlv.children[1] if len(tlv.children) > 1 else None
    return oid, params


def _parse_spki(tlv: TLV) -> Dict:
    tlv.expect(TAG_SEQUENCE)
    _require(len(tlv.children) == 2,
             "SubjectPublicKeyInfo 应有 2 个子元素")
    alg_oid, alg_params = _parse_algorithm_identifier(tlv.children[0])
    alg_name = name_of(alg_oid)
    bit_string = parse_bit_string(tlv.child(1, TAG_BIT_STRING))

    info = {"algorithm": alg_name, "algorithm_oid": alg_oid, "params": {}}
    if alg_oid == "1.2.840.113549.1.1.1":  # rsaEncryption
        key_seq = der.parse(bit_string)
        key_seq.expect(TAG_SEQUENCE)
        _require(len(key_seq.children) == 2,
                 "RSAPublicKey 应有 2 个子元素")
        modulus = parse_integer(key_seq.child(0, TAG_INTEGER))
        exponent = parse_integer(key_seq.child(1, TAG_INTEGER))
        info["params"] = {
            "modulus": modulus,
            "exponent": exponent,
            "key_size": (modulus.bit_length() + 7) // 8 * 8,
        }
    elif alg_oid == "1.2.840.10045.2.1":  # ecPublicKey
        _require(alg_params is not None,
                 "EC 公钥缺少 namedCurve 参数", code="cert.missing_field")
        curve_oid = parse_oid(alg_params)
        curve = name_of(curve_oid)
        if not bit_string or bit_string[0] != 0x04:
            raise StructureError("仅支持未压缩的 EC 公钥点",
                                 code="cert.unsupported_key")
        coord_len = (len(bit_string) - 1) // 2
        info["params"] = {
            "curve": curve,
            "point_x": int.from_bytes(bit_string[1:1 + coord_len], "big"),
            "point_y": int.from_bytes(bit_string[1 + coord_len:], "big"),
            "key_size": coord_len * 8,
        }
    elif alg_oid in ("1.3.101.112", "1.3.101.113"):  # Ed25519 / Ed448
        info["params"] = {"public_key": bit_string.hex(),
                          "key_size": len(bit_string) * 8}
    else:
        info["params"] = {"raw": bit_string.hex()}
    return info


def _parse_extensions(tlv: TLV) -> Dict:
    """解析 [3] Extensions，返回规整后的扩展字典。"""
    tlv.expect(TAG_SEQUENCE)
    result = {}
    for ext in tlv.children:
        ext.expect(TAG_SEQUENCE)
        _require(len(ext.children) >= 2, "Extension 缺少 OID 或值")
        ext_oid = parse_oid(ext.child(0, TAG_OID))
        idx = 1
        critical = False
        if ext.children[idx].tag == der.TAG_BOOLEAN:
            critical = ext.children[idx].content != b"\x00"
            idx += 1
        _require(idx < len(ext.children),
                 "扩展 %s 缺少 extnValue" % ext_oid)
        value_tlv = ext.child(idx, TAG_OCTET_STRING)
        raw = value_tlv.content
        name = name_of(ext_oid)
        entry = {"critical": critical, "raw": raw}

        try:
            if ext_oid == "2.5.29.19":  # basicConstraints
                seq = der.parse(raw)
                seq.expect(TAG_SEQUENCE)
                entry["ca"] = False
                entry["path_len"] = None
                for child in seq.children:
                    if child.tag == der.TAG_BOOLEAN:
                        entry["ca"] = child.content != b"\x00"
                    elif child.tag == TAG_INTEGER:
                        entry["path_len"] = parse_integer(child)
            elif ext_oid == "2.5.29.15":  # keyUsage
                bits_tlv = der.parse(raw)
                bits = parse_bit_string(bits_tlv)
                usages = []
                for i, usage_name in enumerate(KEY_USAGE_BITS):
                    byte_index, bit_index = i // 8, 7 - (i % 8)
                    if byte_index < len(bits) and bits[byte_index] & (1 << bit_index):
                        usages.append(usage_name)
                entry["usages"] = usages
            elif ext_oid == "2.5.29.37":  # extendedKeyUsage
                seq = der.parse(raw)
                seq.expect(TAG_SEQUENCE)
                entry["usages"] = [
                    EKU_OID_TO_NAME.get(parse_oid(c), parse_oid(c))
                    for c in seq.children
                ]
                entry["usage_oids"] = [parse_oid(c) for c in seq.children]
            elif ext_oid == "2.5.29.17":  # subjectAltName
                seq = der.parse(raw)
                seq.expect(TAG_SEQUENCE)
                names = []
                for gen in seq.children:
                    if gen.tag_class == 2 and gen.tag == 2:  # dNSName [2]
                        names.append("DNS:" + gen.content.decode("ascii"))
                    elif gen.tag_class == 2 and gen.tag == 7:  # iPAddress [7]
                        names.append("IP:" + ".".join(str(b) for b in gen.content))
                    elif gen.tag_class == 2 and gen.tag == 1:  # rfc822Name [1]
                        names.append("email:" + gen.content.decode("ascii"))
                entry["names"] = names
        except DerError as exc:
            raise StructureError("扩展 %s 的值解析失败: %s" % (name, exc.message),
                                 code="cert.bad_extension")
        result[name] = entry
    return result


@dataclass
class Certificate:
    """解析后的证书。"""

    version: int
    serial_number: int
    signature_algorithm_oid: str
    signature_algorithm: str
    issuer: List[Tuple[str, str]]
    subject: List[Tuple[str, str]]
    not_before: "object"
    not_after: "object"
    public_key: Dict
    extensions: Dict = field(default_factory=dict)
    tbs_der: bytes = b""
    signature: bytes = b""
    raw_der: bytes = b""

    # ---- 便捷访问 ----
    @property
    def issuer_str(self) -> str:
        return format_name(self.issuer)

    @property
    def subject_str(self) -> str:
        return format_name(self.subject)

    @property
    def is_ca(self) -> bool:
        bc = self.extensions.get("basicConstraints")
        return bool(bc and bc.get("ca"))

    @property
    def path_len(self):
        bc = self.extensions.get("basicConstraints")
        return bc.get("path_len") if bc else None

    @property
    def key_usage(self) -> Optional[List[str]]:
        ku = self.extensions.get("keyUsage")
        return ku.get("usages") if ku else None

    @property
    def eku(self) -> Optional[List[str]]:
        eku = self.extensions.get("extendedKeyUsage")
        return eku.get("usages") if eku else None

    @property
    def eku_oids(self) -> Optional[List[str]]:
        eku = self.extensions.get("extendedKeyUsage")
        return eku.get("usage_oids") if eku else None

    @property
    def fingerprint_sha256(self) -> str:
        return hashlib.sha256(self.raw_der).hexdigest()

    def to_dict(self) -> Dict:
        pk = {"algorithm": self.public_key["algorithm"],
              "params": dict(self.public_key["params"])}
        for key, value in pk["params"].items():
            if isinstance(value, int):
                pk["params"][key] = str(value)
        return {
            "version": self.version,
            "serial_number": str(self.serial_number),
            "signature_algorithm": self.signature_algorithm,
            "issuer": self.issuer,
            "subject": self.subject,
            "issuer_str": self.issuer_str,
            "subject_str": self.subject_str,
            "not_before": self.not_before.isoformat(),
            "not_after": self.not_after.isoformat(),
            "public_key": pk,
            "is_ca": self.is_ca,
            "path_len": self.path_len,
            "key_usage": self.key_usage,
            "extended_key_usage": self.eku,
            "extensions": {
                name: {k: v for k, v in ext.items() if k != "raw"}
                for name, ext in self.extensions.items()
            },
            "fingerprint_sha256": self.fingerprint_sha256,
        }


def parse_certificate(data: bytes) -> Certificate:
    """解析 DER 编码的 X.509 证书。任何结构问题都抛出 CertError 子类。"""
    top = der.parse(data)
    top.expect(TAG_SEQUENCE)
    _require(len(top.children) == 3,
             "Certificate 应有 3 个子元素（tbs/alg/signature），实际 %d"
             % len(top.children))

    tbs_tlv, outer_alg_tlv, sig_tlv = top.children
    tbs_tlv.expect(TAG_SEQUENCE)
    outer_alg_tlv.expect(TAG_SEQUENCE)
    sig_tlv.expect(TAG_BIT_STRING)

    children = tbs_tlv.children
    idx = 0
    version = 1
    if children and children[0].tag_class == 2 and children[0].tag == 0:
        explicit = children[0]
        _require(explicit.constructed and len(explicit.children) == 1,
                 "version [0] EXPLICIT 结构非法", code="cert.bad_structure")
        version = parse_integer(explicit.children[0]) + 1
        idx = 1

    _require(len(children) - idx >= 6,
             "TBSCertificate 缺少必需字段（至少需要 serial/signature/issuer/"
             "validity/subject/spki 共 6 项，实际 %d 项）" % (len(children) - idx))

    serial = parse_integer(children[idx].expect(TAG_INTEGER)); idx += 1
    inner_alg_oid, _ = _parse_algorithm_identifier(children[idx]); idx += 1
    issuer = parse_name(children[idx]); idx += 1

    validity = children[idx]; idx += 1
    validity.expect(TAG_SEQUENCE)
    _require(len(validity.children) == 2,
             "validity 应包含 notBefore/notAfter 两项")
    not_before = parse_time(validity.children[0])
    not_after = parse_time(validity.children[1])

    subject = parse_name(children[idx]); idx += 1
    public_key = _parse_spki(children[idx]); idx += 1

    extensions = {}
    for extra in children[idx:]:
        if extra.tag_class == 2 and extra.tag == 3 and extra.constructed:
            _require(len(extra.children) == 1,
                     "extensions [3] EXPLICIT 结构非法", code="cert.bad_structure")
            extensions = _parse_extensions(extra.children[0])

    outer_alg_oid, _ = _parse_algorithm_identifier(outer_alg_tlv)
    if outer_alg_oid != inner_alg_oid:
        raise StructureError(
            "内外签名算法不一致（tbs=%s, outer=%s）"
            % (name_of(inner_alg_oid), name_of(outer_alg_oid)),
            code="cert.alg_mismatch")

    signature = parse_bit_string(sig_tlv)

    # tbs_der 需要原始编码：用 header+content 重建
    tbs_der = _encode_tlv_bytes(tbs_tlv)

    return Certificate(
        version=version,
        serial_number=serial,
        signature_algorithm_oid=outer_alg_oid,
        signature_algorithm=name_of(outer_alg_oid),
        issuer=issuer,
        subject=subject,
        not_before=not_before,
        not_after=not_after,
        public_key=public_key,
        extensions=extensions,
        tbs_der=tbs_der,
        signature=signature,
        raw_der=bytes(data),
    )


def _encode_tlv_bytes(tlv: TLV) -> bytes:
    """从解析结果无损重建该 TLV 的 DER 编码（tag+length+content）。"""
    first = (tlv.tag_class << 6) | (0x20 if tlv.constructed else 0)
    if tlv.tag < 31:
        tag_bytes = bytes([first | tlv.tag])
    else:
        encoded = []
        tag = tlv.tag
        encoded.append(tag & 0x7F)
        tag >>= 7
        while tag:
            encoded.append(0x80 | (tag & 0x7F))
            tag >>= 7
        tag_bytes = bytes([first | 0x1F] + list(reversed(encoded)))
    length = tlv.length
    if length < 0x80:
        len_bytes = bytes([length])
    else:
        body = length.to_bytes((length.bit_length() + 7) // 8, "big")
        len_bytes = bytes([0x80 | len(body)]) + body
    return tag_bytes + len_bytes + tlv.content
