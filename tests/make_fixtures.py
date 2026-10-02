"""生成 tests/fixtures/ 下的对拍报文（确定性输出，可重复执行）。

用法：python3 tests/make_fixtures.py
"""

from __future__ import annotations

import base64
import quopri
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
CRLF = "\r\n"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _ew(text: str, charset: str = "utf-8") -> str:
    """生成 RFC 2047 B 编码词。"""
    return f"=?{charset}?b?{_b64(text.encode(charset))}?="


def _fold_base64(data: bytes, width: int = 76) -> str:
    encoded = _b64(data)
    return CRLF.join(encoded[i : i + width] for i in range(0, len(encoded), width))


def nested() -> bytes:
    """多层嵌套：mixed -> alternative(plain+html) + 附件 + message/rfc822。"""
    plain_text = "你好，这是纯文本正文。\n第二行。\n"
    plain_qp = quopri.encodestring(plain_text.encode("utf-8"), quotetabs=True).decode("ascii")
    html_b64 = _fold_base64("<html><body><p>你好，HTML 正文</p></body></html>".encode("utf-8"))
    pdf_bytes = b"%PDF-1.4 fake pdf payload\n" * 4
    pdf_b64 = _fold_base64(pdf_bytes)

    inner = (
        "From: " + _ew("内部发件人") + " <inner@example.com>" + CRLF
        + "To: outer@example.com" + CRLF
        + "Subject: =?utf-8?q?=E5=86=85=E5=B5=8C=E9=82=AE=E4=BB=B6?=" + CRLF
        + "Content-Type: text/plain; charset=utf-8" + CRLF
        + CRLF
        + "这是被转发的内嵌邮件正文。" + CRLF
    )

    lines = [
        "From: " + _ew("你好，世界") + " <alice@example.com>",
        "To: bob@example.com",
        "Subject: " + _ew("多部件嵌套邮件") + " " + _ew("报告"),
        "MIME-Version: 1.0",
        'Content-Type: multipart/mixed; boundary="mix-boundary"',
        "",
        "--mix-boundary",
        'Content-Type: multipart/alternative; boundary="alt-boundary"',
        "",
        "--alt-boundary",
        "Content-Type: text/plain; charset=utf-8",
        "Content-Transfer-Encoding: quoted-printable",
        "",
        plain_qp.rstrip("\r\n"),
        "--alt-boundary",
        "Content-Type: text/html; charset=utf-8",
        "Content-Transfer-Encoding: base64",
        "",
        html_b64,
        "--alt-boundary--",
        "--mix-boundary",
        "Content-Type: application/pdf;",
        " name*=utf-8''%E5%B9%B4%E5%BA%A6%E6%8A%A5%E5%91%8A.pdf",
        "Content-Transfer-Encoding: base64",
        "Content-Disposition: attachment;",
        " filename*=utf-8''%E5%B9%B4%E5%BA%A6%E6%8A%A5%E5%91%8A.pdf",
        "",
        pdf_b64,
        "--mix-boundary",
        "Content-Type: message/rfc822",
        "",
        inner.rstrip("\r\n"),
        "--mix-boundary--",
        "",
    ]
    return CRLF.join(lines).encode("utf-8")


def encoded_headers() -> bytes:
    """编码化头部 + GBK 正文 + 编码词文件名附件。"""
    body = "中文正文，GBK 编码。".encode("gbk")
    head_lines = [
        "From: " + _ew("张三", "gb2312") + " <zhangsan@example.com>",
        "To: =?utf-8?q?=E6=9D=8E=E5=9B=9B?= <lisi@example.com>",
        "Subject: " + _ew("测试头部") + " =?gb2312?q?=B2=E2=CA=D4?=",
        "MIME-Version: 1.0",
        'Content-Type: multipart/mixed; boundary="enc-boundary"',
        "",
        "--enc-boundary",
        "Content-Type: text/plain; charset=gbk",
        "Content-Transfer-Encoding: 8bit",
        "",
    ]
    tail_lines = [
        "--enc-boundary",
        "Content-Type: application/octet-stream",
        "Content-Transfer-Encoding: base64",
        "Content-Disposition: attachment;",
        ' filename="=?utf-8?b?6ZmE5Lu25pWw5o2uLnR4dA==?="',
        "",
        _b64(b"attachment-bytes-123"),
        "--enc-boundary--",
        "",
    ]
    head = (CRLF.join(head_lines) + CRLF).encode("utf-8")
    tail = (CRLF.join(tail_lines)).encode("utf-8")
    return head + body + CRLF.encode("ascii") + tail


def unknown_charset_utf8() -> bytes:
    """声明了不存在的字符集，正文实际是 UTF-8，应降级到 utf-8。"""
    body = "未知字符集，实为 UTF-8。".encode("utf-8")
    raw = (
        "Subject: unknown charset" + CRLF
        + "Content-Type: text/plain; charset=x-no-such-charset" + CRLF
        + "Content-Transfer-Encoding: 8bit" + CRLF
        + CRLF
    ).encode("ascii") + body + CRLF.encode("ascii")
    return raw


def unknown_charset_latin1() -> bytes:
    """声明了不存在的字符集，且不是合法 UTF-8，应最终降级到 latin-1。"""
    body = b"caf\xe9 na\xefve \x80\x81"
    raw = (
        "Subject: latin1 fallback" + CRLF
        + "Content-Type: text/plain; charset=x-bogus" + CRLF
        + "Content-Transfer-Encoding: 8bit" + CRLF
        + CRLF
    ).encode("ascii") + body + CRLF.encode("ascii")
    return raw


def empty() -> bytes:
    return b""


def headers_only() -> bytes:
    return (
        "From: a@example.com" + CRLF
        + "To: b@example.com" + CRLF
        + "Subject: headers only" + CRLF
        + CRLF
    ).encode("ascii")


def boundary_in_body() -> bytes:
    """正文里出现形似分隔符的行，不能被当作真实边界。"""
    body_lines = [
        "正文开始。",
        "--frontier42x",
        "-- frontier42",
        " --frontier42",
        "--frontier43--",
        "正文结束。",
    ]
    lines = [
        "Subject: boundary in body",
        "MIME-Version: 1.0",
        'Content-Type: multipart/mixed; boundary="frontier42"',
        "",
        "--frontier42",
        "Content-Type: text/plain; charset=utf-8",
        "",
        *body_lines,
        "--frontier42",
        "Content-Type: text/plain; charset=utf-8",
        "",
        "第二个部分。",
        "--frontier42--",
        "",
    ]
    return CRLF.join(lines).encode("utf-8")


def long_header() -> bytes:
    """超长折叠头部：编码词 Subject 与长 X-Comment，均跨多行折叠。"""
    word = "=?utf-8?b?6LaF6ZW/5qCH6aKY?="  # “超长标题”
    subject_parts = [word] * 1200  # 解码后约 14400 字符
    subject = ("Subject: " + (CRLF + " ").join(subject_parts))
    comment_value = "x" * 8000
    comment = "X-Comment: " + (CRLF + "\t").join(
        comment_value[i : i + 70] for i in range(0, len(comment_value), 70)
    )
    raw = (
        subject + CRLF
        + comment + CRLF
        + "Content-Type: text/plain; charset=utf-8" + CRLF
        + CRLF
        + "body" + CRLF
    )
    return raw.encode("ascii")


FIXTURE_BUILDERS = {
    "nested.eml": nested,
    "encoded_headers.eml": encoded_headers,
    "unknown_charset_utf8.eml": unknown_charset_utf8,
    "unknown_charset_latin1.eml": unknown_charset_latin1,
    "empty.eml": empty,
    "headers_only.eml": headers_only,
    "boundary_in_body.eml": boundary_in_body,
    "long_header.eml": long_header,
}


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, builder in FIXTURE_BUILDERS.items():
        (FIXTURES / name).write_bytes(builder())
        print(f"wrote {FIXTURES / name}")


if __name__ == "__main__":
    main()
