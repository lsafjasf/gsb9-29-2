#!/usr/bin/env python3
"""等长改写证书的 notBefore/notAfter（UTCTime/GeneralizedTime）。

只做同类型、同长度字节替换，因此证书的 DER 长度与签名字节不变，
除有效期外其他字段仍合法，可用于构造“已过期/未生效”对拍样本。

用法: python3 fix_validity.py cert.pem YYMMDDHHMMSSZ YYMMDDHHMMSSZ
      python3 fix_validity.py cert.der  YYMMDDHHMMSSZ YYMMDDHHMMSSZ --der
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from certlib import der
from certlib.der import TAG_GENERALIZEDTIME, TAG_UTCTIME

PEM_BEGIN = "-----BEGIN CERTIFICATE-----"
PEM_END = "-----END CERTIFICATE-----"


def load(path):
    text = path.read_text(errors="replace")
    if PEM_BEGIN in text:
        import base64
        body = text.split(PEM_BEGIN, 1)[1].split(PEM_END, 1)[0]
        return base64.b64decode("".join(body.split())), "pem"
    return path.read_bytes(), "der"


def save(path, data, mode):
    import base64
    if mode == "pem":
        b64 = base64.encodebytes(data).decode().strip()
        path.write_text(PEM_BEGIN + "\n" + b64 + "\n" + PEM_END + "\n")
    else:
        path.write_bytes(data)


def main(argv):
    if len(argv) != 4:
        print(__doc__)
        return 2
    path = Path(argv[1])
    new_nb, new_na = argv[2].encode(), argv[3].encode()
    data, mode = load(path)

    top = der.parse(data)
    tbs = top.child(0, der.TAG_SEQUENCE)
    children = tbs.children
    start_idx = 1 if (children and children[0].tag_class == 2
                      and children[0].tag == 0) else 0
    validity = children[start_idx + 3]
    validity.expect(der.TAG_SEQUENCE)
    nb_tlv, na_tlv = validity.children
    if nb_tlv.tag not in (TAG_UTCTIME, TAG_GENERALIZEDTIME):
        raise SystemExit("notBefore 不是时间类型")
    if len(nb_tlv.content) != len(new_nb) or len(na_tlv.content) != len(new_na):
        raise SystemExit("新时间字符串长度必须与原编码一致（UTCTime 13 字节）")

    buf = bytearray(data)
    buf[nb_tlv.content_offset:nb_tlv.content_offset + len(new_nb)] = new_nb
    buf[na_tlv.content_offset:na_tlv.content_offset + len(new_na)] = new_na
    # 解析一次确认仍是合法 DER
    der.parse(bytes(buf))
    save(path, bytes(buf), mode)
    print("已改写 %s: %s ~ %s" % (argv[1], argv[2], argv[3]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
