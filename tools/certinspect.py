"""命令行：解析并打印证书字段，或校验一条证书链。仅用标准库。

用法:
  python3 tools/inspect.py cert.pem [cert2.pem ...]
    - 只给一张：打印解析出的主体/签发者/有效期/用途/公钥参数(JSON)。
    - 给多张：按 [末端, 中间..., 根] 顺序做链式校验，打印每个问题及
      失败分类 code（退出码：全部通过 0，否则 1，解析失败 2）。
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from certlib import (
    CertError,
    build_chain,
    parse_certificate,
)


def load_cert(path: str) -> bytes:
    raw = Path(path).read_bytes()
    if raw.lstrip().startswith(b"-----BEGIN"):
        body = b"".join(
            line for line in raw.splitlines()
            if not line.startswith(b"-----BEGIN")
            and not line.startswith(b"-----END")
        )
        return base64.b64decode(body)
    return raw


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    try:
        certs = [parse_certificate(load_cert(p)) for p in argv]
    except CertError as exc:
        print(f"解析失败 {exc}", file=sys.stderr)
        return 2

    if len(certs) == 1:
        print(json.dumps(certs[0].to_dict(), indent=2, ensure_ascii=False))
        return 0

    report = build_chain(certs)
    if report.ok:
        print(f"链校验通过：{len(certs)} 张证书")
        return 0
    for issue in report.issues:
        print(issue)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
