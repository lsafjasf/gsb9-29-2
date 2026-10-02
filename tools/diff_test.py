"""对拍：certlib 解析结果 vs OpenSSL 参照解析（ssl._ssl._test_decode_cert）。

对 fixtures/ 下所有“结构合法”的证书（manifest 中 expect=ok 或校验类
失败但编码合法的证书），逐字段比较：
  subject / issuer / serialNumber / notBefore / notAfter
任何不一致都会打印并导致非零退出码。

用法: python3 tools/diff_test.py
"""

from __future__ import annotations

import json
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from certlib import parse_certificate

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

# OpenSSL 长名 -> 我们的短名
LONG_TO_SHORT = {
    "countryName": "C",
    "localityName": "L",
    "stateOrProvinceName": "ST",
    "organizationName": "O",
    "organizationalUnitName": "OU",
    "commonName": "CN",
    "emailAddress": "emailAddress",
}

# 编码合法、可与参照对拍的固件（校验失败不影响字段提取）
DIFFABLE = [
    "root_ca", "intermediate_ca", "leaf_server", "leaf_client",
    "leaf_expired", "leaf_not_yet_valid", "leaf_client_as_server",
    "rogue_root", "leaf_bad_signature", "leaf_issued_by_leaf",
]


def ref_name_to_dict(ref_name) -> dict[str, str]:
    out = {}
    for rdn in ref_name:
        for attr, value in rdn:
            out[LONG_TO_SHORT.get(attr, attr)] = value
    return out


def parse_ref_time(text: str) -> datetime:
    return datetime.strptime(text, "%b %d %H:%M:%S %Y GMT").replace(
        tzinfo=timezone.utc
    )


def compare(name: str) -> list[str]:
    problems = []
    der = (FIXTURES / f"{name}.der").read_bytes()
    pem = FIXTURES / f"{name}.pem"
    ours = parse_certificate(der)
    ref = ssl._ssl._test_decode_cert(str(pem))

    ours_subject = ours.subject.to_dict()
    ref_subject = ref_name_to_dict(ref["subject"])
    if ours_subject != ref_subject:
        problems.append(f"subject 不一致: ours={ours_subject} ref={ref_subject}")

    ours_issuer = ours.issuer.to_dict()
    ref_issuer = ref_name_to_dict(ref["issuer"])
    if ours_issuer != ref_issuer:
        problems.append(f"issuer 不一致: ours={ours_issuer} ref={ref_issuer}")

    ref_serial = int(ref["serialNumber"], 16)
    if ours.serial_number != ref_serial:
        problems.append(
            f"serialNumber 不一致: ours={ours.serial_number:X} "
            f"ref={ref['serialNumber']}"
        )

    if ours.not_before != parse_ref_time(ref["notBefore"]):
        problems.append(
            f"notBefore 不一致: ours={ours.not_before} ref={ref['notBefore']}"
        )
    if ours.not_after != parse_ref_time(ref["notAfter"]):
        problems.append(
            f"notAfter 不一致: ours={ours.not_after} ref={ref['notAfter']}"
        )
    return problems


def main() -> int:
    failed = 0
    for name in DIFFABLE:
        try:
            problems = compare(name)
        except Exception as exc:  # noqa: BLE001 - 对拍工具需要捕获一切
            problems = [f"解析异常: {exc!r}"]
        if problems:
            failed += 1
            print(f"[FAIL] {name}")
            for p in problems:
                print(f"       - {p}")
        else:
            print(f"[ OK ] {name}")
    print(f"\n对拍完成: {len(DIFFABLE) - failed}/{len(DIFFABLE)} 一致")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
