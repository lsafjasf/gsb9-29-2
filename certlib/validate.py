"""证书校验：有效期、用途匹配、链式签发关系。

校验不直接抛异常短路，而是把每个问题都收集进 ValidationReport，
每个问题带有 (code, 证书主体, 原因)，方便调用方区分失败类别。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .errors import CertError, FailureCode, StructureError, ValidationError
from .x509 import (
    Certificate,
    OID_ANY_EKU,
    verify_rsa_signature,
)


@dataclass(frozen=True)
class Issue:
    code: FailureCode
    cert_subject: str
    reason: str

    def __str__(self) -> str:
        return f"[{self.code.value}] {self.cert_subject}: {self.reason}"


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def codes(self) -> list[FailureCode]:
        return [i.code for i in self.issues]

    def add(self, code: FailureCode, cert: Certificate | str, reason: str) -> None:
        subject = str(cert.subject) if isinstance(cert, Certificate) else cert
        self.issues.append(Issue(code, subject, reason))

    def raise_if_any(self) -> None:
        if self.issues:
            raise ValidationError(
                self.issues[0].code, "; ".join(str(i) for i in self.issues)
            )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def check_validity(cert: Certificate, now: datetime | None = None,
                   report: ValidationReport | None = None) -> None:
    now = now or _now()
    if now < cert.not_before:
        msg = (
            f"证书尚未生效：notBefore={cert.not_before.isoformat()} "
            f"> 当前 {now.isoformat()}"
        )
        if report is None:
            raise ValidationError(FailureCode.NOT_YET_VALID, msg)
        report.add(FailureCode.NOT_YET_VALID, cert, msg)
    elif now > cert.not_after:
        msg = (
            f"证书已过期：notAfter={cert.not_after.isoformat()} "
            f"< 当前 {now.isoformat()}"
        )
        if report is None:
            raise ValidationError(FailureCode.EXPIRED, msg)
        report.add(FailureCode.EXPIRED, cert, msg)


def check_usage(cert: Certificate, required_key_usages: set[str] | None = None,
                required_ekus: set[str] | None = None,
                report: ValidationReport | None = None) -> None:
    """检查 keyUsage 位与扩展用途 EKU 是否覆盖所需用途。

    - required_key_usages: 要求全部包含的 keyUsage 位名。
    - required_ekus: 给定用途（如 serverAuth），证书 EKU 须包含该用途
      或包含 anyExtendedKeyUsage；证书没有 EKU 扩展视为用途未声明。
    """
    required_key_usages = required_key_usages or set()
    required_ekus = required_ekus or set()
    reasons = []

    if required_key_usages:
        ku = cert.extensions.key_usage or frozenset()
        missing = required_key_usages - ku
        if missing:
            reasons.append(f"缺少 keyUsage 位 {sorted(missing)}")

    if required_ekus:
        eku = cert.extensions.extended_key_usage
        if eku is None:
            reasons.append(f"未声明 extendedKeyUsage，需要 {sorted(required_ekus)}")
        elif OID_ANY_EKU not in eku:
            missing = required_ekus - eku
            if missing:
                reasons.append(f"extendedKeyUsage 缺少 {sorted(missing)}")

    if reasons:
        msg = "用途不匹配：" + "；".join(reasons)
        if report is None:
            raise ValidationError(FailureCode.USAGE_MISMATCH, msg)
        report.add(FailureCode.USAGE_MISMATCH, cert, msg)


def check_is_ca(cert: Certificate, report: ValidationReport) -> None:
    if cert.extensions.basic_constraints_ca is not True:
        report.add(
            FailureCode.NOT_A_CA, cert,
            "basicConstraints 中 cA 非 TRUE，不能签发下级证书",
        )


def check_issuance(child: Certificate, issuer: Certificate,
                   report: ValidationReport) -> None:
    """检查单张证书由 issuer 签发：名称链接、CA 身份、签名验证。"""
    if child.issuer != issuer.subject:
        report.add(
            FailureCode.ISSUER_MISMATCH, child,
            f"issuer({child.issuer}) 与签发者 subject({issuer.subject}) 不一致",
        )
        return  # 名称都对不上，后续检查没有意义

    check_is_ca(issuer, report)
    try:
        ok = verify_rsa_signature(child, issuer.public_key)
    except StructureError as exc:
        report.add(exc.code, child, str(exc))
        return
    if not ok:
        report.add(
            FailureCode.SIGNATURE_INVALID, child,
            f"签名无法用签发者 {issuer.subject} 的公钥验证",
        )


def build_chain(chain: list[Certificate], now: datetime | None = None,
                required_key_usages: set[str] | None = None,
                required_ekus: set[str] | None = None) -> ValidationReport:
    """校验有序证书链（chain[0] 为末端证书，末尾为自签名根/信任锚）。

    覆盖：
      1. 每张证书的有效期（NOT_YET_VALID / EXPIRED）；
      2. 末端证书的 keyUsage / EKU 用途匹配（USAGE_MISMATCH）；
      3. 相邻证书的 subject/issuer 名称链接（ISSUER_MISMATCH）；
      4. 签发者必须 cA=TRUE（NOT_A_CA）且签名验证通过（SIGNATURE_INVALID）；
      5. basicConstraints pathLenConstraint 深度限制（PATH_LENGTH_EXCEEDED）；
      6. 末尾信任锚必须自签名且自验通过。
    """
    report = ValidationReport()
    now = now or _now()
    if not chain:
        report.add(FailureCode.MISSING_FIELD, "(空)", "证书链为空")
        return report

    for cert in chain:
        check_validity(cert, now, report)

    check_usage(chain[0], required_key_usages, required_ekus, report)

    for j in range(1, len(chain)):
        child, issuer = chain[j - 1], chain[j]
        if child.issuer != issuer.subject:
            report.add(
                FailureCode.ISSUER_MISMATCH, child,
                f"issuer({child.issuer}) 与签发者 subject({issuer.subject}) 不一致",
            )
            continue

        check_is_ca(issuer, report)
        try:
            ok = verify_rsa_signature(child, issuer.public_key)
        except StructureError as exc:
            report.add(exc.code, child, str(exc))
            continue
        if not ok:
            report.add(
                FailureCode.SIGNATURE_INVALID, child,
                f"签名无法用签发者 {issuer.subject} 的公钥验证",
            )
            continue

        # issuer 在链中的下标为 j，其下方（非自签）中间 CA 数量为 j-1：
        # chain[1..j-1] 是中间 CA，chain[0] 是末端实体证书。
        intermediates_below = j - 1
        if issuer.extensions.path_len is not None and (
            intermediates_below > issuer.extensions.path_len
        ):
            report.add(
                FailureCode.PATH_LENGTH_EXCEEDED, issuer,
                f"pathLenConstraint={issuer.extensions.path_len}，"
                f"其下中间 CA 数量为 {intermediates_below}",
            )

    anchor = chain[-1]
    if anchor.subject != anchor.issuer:
        report.add(
            FailureCode.ISSUER_MISMATCH, anchor,
            f"链末信任锚不是自签名（issuer={anchor.issuer}）",
        )
    else:
        try:
            ok = verify_rsa_signature(anchor, anchor.public_key)
        except StructureError as exc:
            report.add(exc.code, anchor, f"信任锚自验失败: {exc}")
        else:
            if not ok:
                report.add(
                    FailureCode.SIGNATURE_INVALID, anchor,
                    "自签名信任锚自验签名失败",
                )
    return report
