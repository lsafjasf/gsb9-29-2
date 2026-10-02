"""纯标准库的签名验证（RSA PKCS#1 v1.5 与 ECDSA）。

只使用 hashlib + 大整数模运算，不依赖 openssl/PyCA，保证解析器
与验证器的“字段读取”逻辑独立可信。
"""

import hashlib

from .der import (
    TAG_BIT_STRING, TAG_INTEGER, TAG_NULL, TAG_OCTET_STRING,
    TAG_SEQUENCE, parse, parse_bit_string, parse_integer,
)
from .errors import StructureError
from .oids import name_of

# DigestInfo 中 AlgorithmIdentifier 的 DER 编码前缀（PKCS#1 v2.1 附录 B.1）
_RSA_DIGEST_PREFIX = {
    "sha1": bytes.fromhex("3021300906052b0e03021a05000414"),
    "sha224": bytes.fromhex("302d300d06096086480165030402040500041c"),
    "sha256": bytes.fromhex("3031300d060960864801650304020105000420"),
    "sha384": bytes.fromhex("3041300d060960864801650304020205000430"),
    "sha512": bytes.fromhex("3051300d060960864801650304020305000440"),
}

# (p, a, b, gx, gy, n)
_SECP256R1 = (
    0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF,
    -3,
    0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B,
    0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
    0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551,
)
_SECP384R1 = (
    0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFFFF0000000000000000FFFFFFFF,
    -3,
    0xB3312FA7E23EE7E4988E056BE3F82D19181D9C6EFE8141120314088F5013875AC656398D8A2ED19D2A85C8EDD3EC2AEF,
    0xAA87CA22BE8B05378EB1C71EF320AD746E1D3B628BA79B9859F741E082542A385502F25DBF55296C3A545E3872760AB7,
    0x3617DE4A96262C6F5D9E98BF9292DC29F8F41DBD289A147CE9DA3113B5F0B8C00A60B1CE1D7E819D7A431D7C90EA0E5F,
    0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFC7634D81F4372DDF581A0DB248B0A77AECEC196ACCC52973,
)
_SECP521R1 = (
    0x01FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF,
    -3,
    0x0051953EB9618E1C9A1F929A21A0B68540EEA2DA725B99B315F3B8B489918EF109E156193951EC7E937B1652C0BD3BB1BF073573DF883D2C34F1EF451FD46B503F00,
    0x00C6858E06B70404E9CD9E3ECB662395B4429C648139053FB521F828AF606B4D3DBAA14B5E77EFE75928FE1DC127A2FFA8DE3348B3C1856A429BF97E7E31C2E5BD66,
    0x011839296A789A3BC0045C8A5FB42C7D1BD998F54449579B446817AFBD17273E662C97EE72995EF42640C550B9013FAD0761353C7086A272C24088BE94769FD16650,
    0x01FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFA51868783BF2F966B7FCC0148F709A5D03BB5C9B8899C47AEBB6FB71E91386409,
)
_SECP256K1 = (
    0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F,
    0,
    7,
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
    0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141,
)

_CURVES = {
    "secp256r1": _SECP256R1,
    "secp384r1": _SECP384R1,
    "secp521r1": _SECP521R1,
    "secp256k1": _SECP256K1,
}

# 证书签名 OID -> (族, 摘要名)
_SIG_ALGS = {
    "1.2.840.113549.1.1.5": ("rsa", "sha1"),
    "1.2.840.113549.1.1.11": ("rsa", "sha256"),
    "1.2.840.113549.1.1.12": ("rsa", "sha384"),
    "1.2.840.113549.1.1.13": ("rsa", "sha512"),
    "1.2.840.113549.1.1.14": ("rsa", "sha224"),
    "1.2.840.10045.4.1": ("ecdsa", "sha1"),
    "1.2.840.10045.4.3.2": ("ecdsa", "sha256"),
    "1.2.840.10045.4.3.3": ("ecdsa", "sha384"),
    "1.2.840.10045.4.3.4": ("ecdsa", "sha512"),
    "1.3.101.112": ("ed25519", None),
}


def sig_alg_info(sig_oid: str):
    """返回 (族, 摘要名)；不支持的算法返回 None。"""
    return _SIG_ALGS.get(sig_oid)


# ---------- RSA ----------

def _rsa_verify(pk_info, message: bytes, signature: bytes, digest_name: str):
    """按 PKCS#1 v1.5 EMSA-PKCS1-v1_5 验证。失败返回字符串原因，成功返回 None。"""
    spki = pk_info
    if spki["algorithm"] != "rsaEncryption":
        return "签发者公钥不是 RSA（实际 %s），无法验证 RSA 签名" % spki["algorithm"]
    n = spki["params"]["modulus"]
    e = spki["params"]["exponent"]
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return "RSA 签名长度 %d 与模长 %d 不一致" % (len(signature), k)

    encoded = pow(int.from_bytes(signature, "big"), e, n)
    em = encoded.to_bytes(k, "big")

    prefix = _RSA_DIGEST_PREFIX[digest_name]
    digest = hashlib.new(digest_name, message).digest()
    t = prefix + digest
    if len(em) < len(t) + 11:
        return "EM 长度不足以容纳 DigestInfo"
    pad = b"\xff" * (k - len(t) - 3)
    expected = b"\x00\x01" + pad + b"\x00" + t
    if em != expected:
        return "RSA PKCS#1 v1.5 填充或摘要不匹配"
    return None


# ---------- ECDSA ----------

def _ec_inv(x, n):
    return pow(x, -1, n)


def _ec_add(P, Q, p, a):
    if P is None:
        return Q
    if Q is None:
        return P
    x1, y1 = P
    x2, y2 = Q
    if x1 == x2 and (y1 + y2) % p == 0:
        return None
    if P == Q:
        if y1 % p == 0:
            return None
        m = (3 * x1 * x1 + a) * _ec_inv(2 * y1, p) % p
    else:
        m = (y2 - y1) * _ec_inv((x2 - x1) % p, p) % p
    x3 = (m * m - x1 - x2) % p
    y3 = (m * (x1 - x3) - y1) % p
    return (x3, y3)


def _ec_mul(k, point, p, a):
    result = None
    addend = point
    while k:
        if k & 1:
            result = _ec_add(result, addend, p, a)
        addend = _ec_add(addend, addend, p, a)
        k >>= 1
    return result


def _ecdsa_verify(pk_info, message: bytes, signature: bytes, digest_name: str):
    if pk_info["algorithm"] != "ecPublicKey":
        return "签发者公钥不是 ECDSA（实际 %s）" % pk_info["algorithm"]
    curve_name = pk_info["params"]["curve"]
    if curve_name not in _CURVES:
        return "不支持的曲线 %s（仅 %s）" % (curve_name, ", ".join(sorted(_CURVES)))
    p, a, _b, gx, gy, n = _CURVES[curve_name]
    qx = pk_info["params"]["point_x"]
    qy = pk_info["params"]["point_y"]
    if not qx or not (0 <= qx < p and 0 <= qy < p):
        return "EC 公钥点坐标越界"

    try:
        seq = parse(signature)
        seq.expect(TAG_SEQUENCE)
        if len(seq.children) != 2:
            return "ECDSA 签名应包含 2 个 INTEGER"
        r = parse_integer(seq.child(0, TAG_INTEGER))
        s = parse_integer(seq.child(1, TAG_INTEGER))
    except Exception as exc:
        return "ECDSA 签名 DER 解析失败: %s" % exc
    if not (1 <= r < n and 1 <= s < n):
        return "ECDSA 的 r/s 越界"

    point = (qx, qy)
    digest = hashlib.new(digest_name, message).digest()
    z = int.from_bytes(digest, "big")
    # 摘要长于 n 时截断高位（FIPS 186-4 sec 4.2）
    zbits = n.bit_length()
    if z.bit_length() > zbits:
        z >>= (z.bit_length() - zbits)
    w = _ec_inv(s, n)
    u1 = (z * w) % n
    u2 = (r * w) % n
    point1 = _ec_mul(u1, (gx, gy), p, a)
    point2 = _ec_mul(u2, point, p, a)
    total = _ec_add(point1, point2, p, a)
    if total is None:
        return "ECDSA 校验得到无穷远点"
    x, _y = total
    if (x % n) != r:
        return "ECDSA 签名校验失败（r 不匹配）"
    return None


def verify_signature(issuer_pk_info, sig_oid: str, tbs_der: bytes,
                     signature: bytes, sig_null_check=True):
    """验证 tbs_der 上的签名。

    返回 (ok: bool, reason: str|None)。对暂不支持的算法（PSS/Ed25519）
    返回 (None, "unsupported: ...")，由调用方决定如何处理。
    """
    info = sig_alg_info(sig_oid)
    if info is None:
        return None, "unsupported: 未知签名算法 OID %s(%s)" % (sig_oid, name_of(sig_oid))
    family, digest_name = info
    if family == "ed25519":
        return None, "unsupported: Ed25519 验证未实现（OID %s）" % sig_oid
    try:
        if family == "rsa":
            reason = _rsa_verify(issuer_pk_info, tbs_der, signature, digest_name)
        elif family == "ecdsa":
            reason = _ecdsa_verify(issuer_pk_info, tbs_der, signature, digest_name)
        else:
            return None, "unsupported: %s" % family
    except StructureError as exc:
        return False, str(exc)
    return (reason is None), reason
