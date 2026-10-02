"""证书校验：有效期、用途、链式签发关系。

所有 check_* 函数返回 CertError 列表（空列表 = 通过），
错误对象带 code 便于分类统计与断言。
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional

from .crypto import sig_alg_info, verify_signature
from .errors import ChainError, CertError, PurposeError, ValidityError
from .oids import ANY_EKU, PURPOSE_TO_EKU
from .x509 import Certificate

# 用途 -> 需要的 keyUsage 位（仅对 CA 强制 keyCertSign；其余见 PURPOSE_REQUIRED_KU）
PURPOSE_REQUIRED_KU = {
    "serverAuth": {"digitalSignature", "keyEncipherment", "keyAgreement"},
    "clientAuth": {"digitalSignature", "keyAgreement"},
    "codeSigning": {"digitalSignature"},
    "emailProtection": {"digitalSignature", "nonRepudiation",
                        "keyEncipherment", "keyAgreement"},
    "timeStamping": {"digitalSignature"},
    "OCSPSigning": {"digitalSignature"},
}


def _now(at: Optional[datetime]) -> datetime:
    if at is None:
        return datetime.now(timezone.utc)
    if at.tzinfo is None:
        return at.replace(tzinfo=timezone.utc)
    return at


def check_validity(cert: Certificate, at: Optional[datetime] = None) -> List[CertError]:
    """有效期校验：区分“尚未生效”与“已过期”。"""
    moment = _now(at)
    errors = []
    if moment < cert.not_before:
        errors.append(ValidityError(
            "证书尚未生效：当前 %s，生效时间 %s"
            % (moment.isoformat(), cert.not_before.isoformat()),
            code="validity.not_yet_valid"))
    if moment > cert.not_after:
        errors.append(ValidityError(
            "证书已过期：当前 %s，失效时间 %s"
            % (moment.isoformat(), cert.not_after.isoformat()),
            code="validity.expired"))
    return errors


def check_purpose(cert: Certificate, purpose: str) -> List[CertError]:
    """用途校验：EKU 与 keyUsage 双重匹配。

    purpose 取值：serverAuth / clientAuth / codeSigning / emailProtection /
    timeStamping / OCSPSigning / ca。
    """
    errors = []
    if purpose == "ca":
        if not cert.is_ca:
            errors.append(PurposeError(
                "basicConstraints 未声明 CA=TRUE，不能作为 CA 使用",
                code="purpose.not_ca"))
        ku = cert.key_usage
        if ku is not None and "keyCertSign" not in ku:
            errors.append(PurposeError(
                "CA 证书 keyUsage 缺少 keyCertSign", code="purpose.ku_missing"))
        return errors

    eku_oids = cert.eku_oids
    if eku_oids is not None:
        needed = PURPOSE_TO_EKU.get(purpose)
        if needed is None:
            errors.append(PurposeError("未知用途 %s" % purpose,
                                       code="purpose.unknown"))
        elif needed not in eku_oids and ANY_EKU not in eku_oids:
            errors.append(PurposeError(
                "EKU 不包含 %s（实际：%s）"
                % (purpose, ", ".join(cert.eku or [])),
                code="purpose.eku_mismatch"))

    ku = cert.key_usage
    if ku is not None:
        required = PURPOSE_REQUIRED_KU.get(purpose, set())
        if required and not (required & set(ku)):
            errors.append(PurposeError(
                "keyUsage %s 不满足用途 %s（需要 %s 之一）"
                % (sorted(ku), purpose, sorted(required)),
                code="purpose.ku_missing"))
    return errors


def check_issued_by(cert: Certificate, issuer: Certificate) -> List[CertError]:
    """检查 cert 是否由 issuer 签发：名称匹配 + 签名验证。"""
    errors = []
    if cert.issuer != issuer.subject:
        errors.append(ChainError(
            "签发者名称不匹配：证书 issuer 为 [%s]，上级 subject 为 [%s]"
            % (cert.issuer_str, issuer.subject_str),
            code="chain.issuer_mismatch"))
        return errors  # 名称都不匹配，签名验证无意义

    ok, reason = verify_signature(
        issuer.public_key, cert.signature_algorithm_oid,
        cert.tbs_der, cert.signature)
    if ok is None:
        errors.append(ChainError(
            "签名算法 %s 暂不支持验证（%s），无法确认签发关系"
            % (cert.signature_algorithm, reason),
            code="chain.sig_unsupported"))
    elif not ok:
        errors.append(ChainError(
            "签名验证失败：%s（算法 %s）" % (reason, cert.signature_algorithm),
            code="chain.bad_signature"))
    return errors


def verify_chain(chain: List[Certificate],
                 purpose: Optional[str] = None,
                 at: Optional[datetime] = None,
                 trusted_roots: Optional[List[Certificate]] = None) -> List[CertError]:
    """校验整条链。chain[0] 为叶子，其后依次为上级 CA。

    - 每一级检查有效期、签发关系（名称+签名）、上级是否为 CA、pathlen；
    - 叶子按 purpose 检查用途；
    - 若提供 trusted_roots，则链顶必须落在信任根内（按指纹或 subject+公钥）。
    """
    errors: List[CertError] = []
    if not chain:
        return [ChainError("证书链为空", code="chain.empty")]

    leaf = chain[0]
    errors.extend(check_validity(leaf, at))
    if purpose:
        errors.extend(check_purpose(leaf, purpose))

    ca_depth = 0
    for i in range(len(chain) - 1):
        child, issuer = chain[i], chain[i + 1]
        errors.extend(check_validity(issuer, at))
        if not issuer.is_ca:
            errors.append(ChainError(
                "链中第 %d 级 [%s] 不是 CA（basicConstraints 未置 CA）"
                % (i + 1, issuer.subject_str),
                code="chain.not_ca"))
        else:
            path_len = issuer.path_len
            if path_len is not None and ca_depth > path_len:
                errors.append(ChainError(
                    "链中第 %d 级 [%s] 超出 pathlen 限制（pathlen=%d，"
                    "其下 CA 层级=%d）"
                    % (i + 1, issuer.subject_str, path_len, ca_depth),
                    code="chain.pathlen_exceeded"))
            ca_depth += 1
        errors.extend(check_issued_by(child, issuer))

    top = chain[-1]
    if len(chain) == 1:
        errors.extend(check_validity(top, at))
    if trusted_roots is not None:
        trusted_fps = {root.fingerprint_sha256 for root in trusted_roots}
        trusted_keys = {
            (tuple(root.subject), root.public_key["algorithm"],
             str(sorted(root.public_key["params"].items())))
            for root in trusted_roots
        }
        top_key = (tuple(top.subject), top.public_key["algorithm"],
                   str(sorted(top.public_key["params"].items())))
        if top.fingerprint_sha256 not in trusted_fps and top_key not in trusted_keys:
            errors.append(ChainError(
                "链顶 [%s] 不在信任根列表中" % top.subject_str,
                code="chain.untrusted_root"))
    return errors
