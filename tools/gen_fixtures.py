"""生成对拍与自测用的证书固件（纯标准库，不依赖 openssl 命令）。

- 用 Miller-Rabin 生成 RSA 密钥（512 位，足够测试且生成快）；
- 用 certlib.asn1 的 DER 编码器签发 X.509 v3 证书（sha256WithRSAEncryption）；
- 产出：fixtures/*.der + *.pem + manifest.json。
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from certlib import asn1
from certlib.x509 import (
    OID_BASIC_CONSTRAINTS,
    OID_EXT_KEY_USAGE,
    OID_KEY_USAGE,
    OID_RSA_ENCRYPTION,
    OID_SERVER_AUTH,
    OID_CLIENT_AUTH,
    OID_SHA256_WITH_RSA,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

_rng = random.Random(20261003)


def _is_probable_prime(n: int, rounds: int = 20) -> bool:
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d = n - 1
    r = 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = _rng.randrange(2, n - 2)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def gen_rsa_key(bits: int = 512) -> dict:
    half = bits // 2
    while True:
        p = _rng.getrandbits(half) | (1 << (half - 1)) | 1
        if _is_probable_prime(p):
            break
    while True:
        q = _rng.getrandbits(half) | (1 << (half - 1)) | 1
        if q != p and _is_probable_prime(q):
            break
    n = p * q
    phi = (p - 1) * (q - 1)
    e = 65537
    d = pow(e, -1, phi)
    return {"n": n, "e": e, "d": d}


def rsa_sign_sha256(key: dict, data: bytes) -> bytes:
    digest = hashlib.sha256(data).digest()
    prefix = bytes.fromhex("3031300d060960864801650304020105000420")
    k = (key["n"].bit_length() + 7) // 8
    em = (b"\x00\x01" + b"\xff" * (k - len(prefix) - len(digest) - 3)
          + b"\x00" + prefix + digest)
    s = pow(int.from_bytes(em, "big"), key["d"], key["n"])
    return s.to_bytes(k, "big")


def der_name(attrs: list[tuple[str, str]]) -> bytes:
    oid_map = {"CN": "2.5.4.3", "O": "2.5.4.10", "OU": "2.5.4.11", "C": "2.5.4.6"}
    rdns = b"".join(
        asn1.set_of(asn1.sequence(asn1.oid(oid_map[k]), asn1.utf8_string(v)))
        for k, v in attrs
    )
    return asn1.sequence(rdns)


def der_spki(key: dict) -> bytes:
    rsa_pub = asn1.sequence(asn1.integer(key["n"]), asn1.integer(key["e"]))
    alg = asn1.sequence(asn1.oid(OID_RSA_ENCRYPTION), asn1.null())
    return asn1.sequence(alg, asn1.bit_string(rsa_pub))


def ext_key_usage(bits: list[str], critical: bool = True) -> bytes:
    names = ["digitalSignature", "nonRepudiation", "keyEncipherment",
             "dataEncipherment", "keyAgreement", "keyCertSign", "cRLSign",
             "encipherOnly", "decipherOnly"]
    nbits = max(names.index(b) for b in bits) + 1
    nbytes = (nbits + 7) // 8
    value = bytearray(nbytes)
    for b in bits:
        i = names.index(b)
        value[i // 8] |= 0x80 >> (i % 8)
    unused = nbytes * 8 - nbits
    inner = asn1.bit_string(bytes(value), unused)
    body = asn1.oid(OID_KEY_USAGE)
    if critical:
        body += asn1.boolean(True)
    body += asn1.octet_string(inner)
    return asn1.sequence(body)


def ext_basic_constraints(ca: bool, path_len: int | None = None,
                          critical: bool = True) -> bytes:
    inner = b""
    if ca:
        inner += asn1.boolean(True)
    if path_len is not None:
        inner += asn1.integer(path_len)
    body = asn1.oid(OID_BASIC_CONSTRAINTS)
    if critical:
        body += asn1.boolean(True)
    body += asn1.octet_string(asn1.sequence(inner))
    return asn1.sequence(body)


def ext_eku(eku_oids: list[str]) -> bytes:
    inner = asn1.sequence(b"".join(asn1.oid(o) for o in eku_oids))
    body = (asn1.oid(OID_EXT_KEY_USAGE)
            + asn1.octet_string(inner))
    return asn1.sequence(body)


def fmt_time(dt: datetime) -> bytes:
    dt = dt.astimezone(timezone.utc)
    if 1950 <= dt.year <= 2049:
        return asn1.utc_time(dt.strftime("%y%m%d%H%M%SZ"))
    return asn1.generalized_time(dt.strftime("%Y%m%d%H%M%SZ"))


def build_cert(subject: list[tuple[str, str]], issuer: list[tuple[str, str]],
               subject_key: dict, issuer_key: dict, serial: int,
               not_before: datetime, not_after: datetime,
               extensions: list[bytes], is_ca: bool,
               serial_der: bytes | None = None) -> bytes:
    tbs = asn1.sequence(
        asn1.tlv(0xA0, asn1.integer(2)),  # [0] version = v3
        serial_der if serial_der is not None else asn1.integer(serial),
        asn1.sequence(asn1.oid(OID_SHA256_WITH_RSA), asn1.null()),
        der_name(issuer),
        asn1.sequence(fmt_time(not_before), fmt_time(not_after)),
        der_name(subject),
        der_spki(subject_key),
        asn1.tlv(0xA3, asn1.sequence(*extensions)),
    )
    sig = rsa_sign_sha256(issuer_key, tbs)
    return asn1.sequence(
        tbs,
        asn1.sequence(asn1.oid(OID_SHA256_WITH_RSA), asn1.null()),
        asn1.bit_string(sig),
    )


def to_pem(der: bytes, label: str = "CERTIFICATE") -> str:
    b64 = base64.b64encode(der).decode("ascii")
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"


def main() -> None:
    FIXTURES.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    year = timedelta(days=365)

    root_key = gen_rsa_key()
    inter_key = gen_rsa_key()
    leaf_key = gen_rsa_key()
    client_key = gen_rsa_key()
    rogue_key = gen_rsa_key()

    root_name = [("C", "CN"), ("O", "Test PKI"), ("CN", "Test Root CA")]
    inter_name = [("C", "CN"), ("O", "Test PKI"), ("CN", "Test Intermediate CA")]
    leaf_name = [("C", "CN"), ("O", "Test PKI"), ("CN", "server.example.com")]
    client_name = [("C", "CN"), ("O", "Test PKI"), ("CN", "client-01")]

    manifest: dict[str, dict] = {}

    def emit(name: str, der: bytes, expect: str, note: str) -> None:
        (FIXTURES / f"{name}.der").write_bytes(der)
        (FIXTURES / f"{name}.pem").write_text(to_pem(der))
        manifest[name] = {"expect": expect, "note": note}

    # 1. 自签名根 CA（pathLen=1）
    root_der = build_cert(
        root_name, root_name, root_key, root_key, serial=1,
        not_before=now - year, not_after=now + 10 * year,
        extensions=[ext_basic_constraints(True, path_len=1),
                    ext_key_usage(["keyCertSign", "cRLSign"])],
        is_ca=True,
    )
    emit("root_ca", root_der, "ok", "自签名根 CA，pathLen=1")

    # 2. 中间 CA（pathLen=0）
    inter_der = build_cert(
        inter_name, root_name, inter_key, root_key, serial=2,
        not_before=now - year, not_after=now + 5 * year,
        extensions=[ext_basic_constraints(True, path_len=0),
                    ext_key_usage(["keyCertSign", "cRLSign"])],
        is_ca=True,
    )
    emit("intermediate_ca", inter_der, "ok", "根签发的中间 CA，pathLen=0")

    # 3. 服务器末端证书（keyUsage + EKU serverAuth）
    leaf_der = build_cert(
        leaf_name, inter_name, leaf_key, inter_key, serial=3,
        not_before=now - timedelta(days=30), not_after=now + 2 * year,
        extensions=[ext_basic_constraints(False),
                    ext_key_usage(["digitalSignature", "keyEncipherment"]),
                    ext_eku([OID_SERVER_AUTH])],
        is_ca=False,
    )
    emit("leaf_server", leaf_der, "ok", "服务器证书，EKU=serverAuth")

    # 4. 客户端证书（EKU=clientAuth）
    client_der = build_cert(
        client_name, inter_name, client_key, inter_key, serial=4,
        not_before=now - timedelta(days=30), not_after=now + 2 * year,
        extensions=[ext_basic_constraints(False),
                    ext_key_usage(["digitalSignature"]),
                    ext_eku([OID_CLIENT_AUTH])],
        is_ca=False,
    )
    emit("leaf_client", client_der, "ok", "客户端证书，EKU=clientAuth")

    # 5. 已过期证书
    expired_der = build_cert(
        [("CN", "expired.example.com")], inter_name, leaf_key, inter_key,
        serial=5, not_before=now - 3 * year, not_after=now - year,
        extensions=[ext_basic_constraints(False),
                    ext_key_usage(["digitalSignature"]),
                    ext_eku([OID_SERVER_AUTH])],
        is_ca=False,
    )
    emit("leaf_expired", expired_der, "EXPIRED", "notAfter 已在过去")

    # 6. 尚未生效证书
    future_der = build_cert(
        [("CN", "future.example.com")], inter_name, leaf_key, inter_key,
        serial=6, not_before=now + timedelta(days=30),
        not_after=now + 2 * year,
        extensions=[ext_basic_constraints(False),
                    ext_key_usage(["digitalSignature"]),
                    ext_eku([OID_SERVER_AUTH])],
        is_ca=False,
    )
    emit("leaf_not_yet_valid", future_der, "NOT_YET_VALID", "notBefore 在将来")

    # 7. 用途不符：只有 clientAuth，当服务器证书用应失败
    emit("leaf_client_as_server", client_der, "USAGE_MISMATCH",
         "EKU=clientAuth 却要求 serverAuth")

    # 8. 链断裂：leaf 声称由中间 CA 签发，但链中放 rogue 根
    rogue_root_der = build_cert(
        [("CN", "Rogue Root")], [("CN", "Rogue Root")], rogue_key, rogue_key,
        serial=7, not_before=now - year, not_after=now + year,
        extensions=[ext_basic_constraints(True),
                    ext_key_usage(["keyCertSign"])],
        is_ca=True,
    )
    emit("rogue_root", rogue_root_der, "ok",
         "与 leaf_server 组合时产生 ISSUER_MISMATCH")

    # 9. 签名被篡改的证书（有效结构，签名最后一字节翻转）
    tampered = bytearray(leaf_der)
    tampered[-1] ^= 0x01
    emit("leaf_bad_signature", bytes(tampered), "SIGNATURE_INVALID",
         "签名值被翻转 1 bit")

    # 10. 末端实体充当签发者（cA=FALSE 签发下级）
    fake_leaf_ca_der = build_cert(
        [("CN", "issued-by-leaf.example.com")], leaf_name, client_key, leaf_key,
        serial=8, not_before=now - timedelta(days=10), not_after=now + year,
        extensions=[ext_basic_constraints(False),
                    ext_key_usage(["digitalSignature"])],
        is_ca=False,
    )
    emit("leaf_issued_by_leaf", fake_leaf_ca_der, "NOT_A_CA",
         "由 cA=FALSE 的末端证书签发")

    # ---- 畸形编码固件（字节级损坏，仅 .der）----
    def emit_raw(name: str, der: bytes, expect: str, note: str) -> None:
        (FIXTURES / f"{name}.der").write_bytes(der)
        manifest[name] = {"expect": expect, "note": note}

    # 截断：去掉末尾 20 字节
    emit_raw("malformed_truncated", leaf_der[:-20], "TRUNCATED",
             "文件尾部被截断")
    # 不定长：把顶层 SEQUENCE 长度改成 0x80
    bad = bytearray(leaf_der)
    # 顶层是 0x30 0x82 len_hi len_lo
    if bad[1] == 0x82:
        bad = bytes([0x30, 0x80]) + bytes(bad[4:])
    emit_raw("malformed_indefinite_length", bad, "INDEFINITE_LENGTH",
             "顶层长度改为不定长 0x80")
    # 非最短 INTEGER：序列号编码带冗余前导 0x00（嵌套长度保持一致，
    # 错误应精确落在 NON_MINIMAL_INTEGER）
    nonminimal_der = build_cert(
        leaf_name, inter_name, leaf_key, inter_key, serial=3,
        not_before=now - timedelta(days=30), not_after=now + 2 * year,
        extensions=[ext_basic_constraints(False),
                    ext_key_usage(["digitalSignature"])],
        is_ca=False,
        serial_der=b"\x02\x02\x00\x03",
    )
    emit_raw("malformed_nonminimal_integer", nonminimal_der,
             "NON_MINIMAL_INTEGER", "serialNumber 带冗余前导 0x00")
    # 超长字段：顶层声明巨大长度
    emit_raw("malformed_huge_length",
             b"\x30\x84\xff\xff\xff\xff" + leaf_der[5:60],
             "FIELD_TOO_LONG|TRUNCATED", "顶层声明 4GiB 长度")
    # 多余尾部数据
    emit_raw("malformed_trailing", leaf_der + b"\xde\xad\xbe\xef",
             "TRAILING_DATA", "证书后附加垃圾字节")
    # 缺失必需字段：TBSCertificate 中删掉 subjectPublicKeyInfo 不可行
    # （长度嵌套），改为构造一个缺 validity 的最小 TBS
    tbs_missing = asn1.sequence(
        asn1.tlv(0xA0, asn1.integer(2)),
        asn1.integer(9),
        asn1.sequence(asn1.oid(OID_SHA256_WITH_RSA), asn1.null()),
        der_name(root_name),
        # 缺 validity / subject / spki
    )
    emit_raw("malformed_missing_fields", tbs_missing, "MISSING_FIELD",
             "TBSCertificate 缺少 validity/subject/spki")

    (FIXTURES / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    )
    print(f"已生成 {len(manifest)} 个固件到 {FIXTURES}")


if __name__ == "__main__":
    main()
