# -*- coding: utf-8 -*-
"""对拍语料：正常嵌套报文 + 边界情形。每项为 (名称, 原始报文字节)。"""


def _folded_long_header(total=12000, width=72):
    """生成一个约 total 字节、按 width 折叠的超长头部。"""
    value = b"x" * total
    lines = [b"X-Long-Header: " + value[:width]]
    value = value[width:]
    while value:
        lines.append(b" " + value[:width])
        value = value[width:]
    return b"\r\n".join(lines) + b"\r\n"


NESTED_MIXED = (
    b"From: alice@example.com\r\n"
    b"To: bob@example.com\r\n"
    b"Subject: =?UTF-8?B?5bm25aSW5a+55qGI?= report\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="outer-boundary"\r\n'
    b"\r\n"
    b"Preamble, must be ignored.\r\n"
    b"--outer-boundary\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"Content-Transfer-Encoding: quoted-printable\r\n"
    b"\r\n"
    b"Hello=2C this is =E4=B8=AD=E6=96=87 body.\r\n"
    b"--outer-boundary\r\n"
    b'Content-Type: multipart/alternative; boundary="inner-boundary"\r\n'
    b"\r\n"
    b"--inner-boundary\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"plain version\r\n"
    b"--inner-boundary\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"PGI+aHRtbCB2ZXJzaW9uPC9iPg==\r\n"
    b"--inner-boundary--\r\n"
    b"--outer-boundary\r\n"
    b"Content-Type: application/pdf\r\n"
    b"Content-Transfer-Encoding: base64\r\n"
    b"Content-Disposition: attachment; filename*=utf-8''%E6%8A%A5%E5%91%8A.pdf\r\n"
    b"\r\n"
    b"JVBERi0xLjQK\r\n"
    b"--outer-boundary--\r\n"
    b"Epilogue, must be ignored.\r\n"
)

ENCODED_HEADERS = (
    b"Subject: =?UTF-8?B?5L2g5aW977yM5LiW55WM?=\r\n"
    b" =?GB2312?Q?=C4=FA=BA=C3?= plain tail\r\n"
    b"From: =?UTF-8?Q?=E5=BC=A0=E4=B8=89?= <zhangsan@example.com>\r\n"
    b"X-Unknown-Word: =?X-NO-SUCH-CHARSET?B?6L+Z5piv5rWL6K+V?=\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"body\r\n"
)

BOUNDARY_LIKE_BODY = (
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"--boundary\r\n"
    b"------\r\n"
    b"--outer-boundary--\r\n"
    b"not a multipart message at all\r\n"
)

BOUNDARY_PREFIX_IN_BODY = (
    b'Content-Type: multipart/mixed; boundary="outer"\r\n'
    b"\r\n"
    b"--outer\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"--out\r\n"
    b"--outerx\r\n"
    b"--outer extra\r\n"
    b" --outer\r\n"
    b"----outer\r\n"
    b"--outer\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"second part\r\n"
    b"--outer--\r\n"
)

RFC2231_FILENAME = (
    b'Content-Type: multipart/mixed; boundary="b2231"\r\n'
    b"\r\n"
    b"--b2231\r\n"
    b"Content-Type: application/octet-stream\r\n"
    b"Content-Transfer-Encoding: base64\r\n"
    b"Content-Disposition: attachment;\r\n"
    b" filename*0*=utf-8''%E4%B8%AD%E6%96%87%E6%96%87%E4%BB%B6;\r\n"
    b" filename*1*=%E5%90%8D%E5%AD%97%20%E5%BE%88%E9%95%BF;\r\n"
    b" filename*2*=.bin\r\n"
    b"\r\n"
    b"AAECAwQ=\r\n"
    b"--b2231\r\n"
    b"Content-Type: application/octet-stream\r\n"
    b'Content-Disposition: attachment; filename="quo\\"ted.txt"\r\n'
    b"\r\n"
    b"raw-bytes\r\n"
    b"--b2231--\r\n"
)

UNKNOWN_CHARSET_BODY = (
    b'Content-Type: multipart/mixed; boundary="bcs"\r\n'
    b"\r\n"
    b"--bcs\r\n"
    b"Content-Type: text/plain; charset=x-unknown-9\r\n"
    b"\r\n"
    + "héllo wörld 中文\r\n".encode("utf-8").replace(b"\n", b"\r\n")
    + b"--bcs\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"caf\xe9 au lait\r\n"
    b"--bcs--\r\n"
)

MESSAGE_RFC822 = (
    b'Content-Type: multipart/mixed; boundary="fwd"\r\n'
    b"\r\n"
    b"--fwd\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"see attached\r\n"
    b"--fwd\r\n"
    b"Content-Type: message/rfc822\r\n"
    b"\r\n"
    b"From: carol@example.com\r\n"
    b"Subject: =?UTF-8?B?6L2s5Y+R5L+h?=\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"inner body\r\n"
    b"--fwd--\r\n"
)

QUOTED_BOUNDARY = (
    b'Content-Type: multipart/mixed; boundary="my boundary"\r\n'
    b"\r\n"
    b"--my boundary\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"one\r\n"
    b"--my boundary\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"two\r\n"
    b"--my boundary--\r\n"
)

QP_SOFTPREAK = (
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"Content-Transfer-Encoding: quoted-printable\r\n"
    b"\r\n"
    b"line one=\r\n"
    b"continues here=20with space\r\n"
    b"=E4=B8=AD=E6=96=87=E5=AD=97=E7=AC=A6\r\n"
)

LONG_HEADER = (
    _folded_long_header()
    + b"Subject: =?UTF-8?B?5L2g5aW977yM5LiW55WM5L2g5aW977yM5LiW55WM?=\r\n"
    b" =?UTF-8?B?5L2g5aW977yM5LiW55WM5L2g5aW977yM5LiW55WM?=\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"short body\r\n"
)

MULTIPART_NO_BOUNDARY = (
    b"Content-Type: multipart/mixed\r\n"
    b"\r\n"
    b"body without any boundary\r\n"
)

NO_FINAL_NEWLINE = (
    b'Content-Type: multipart/mixed; boundary="eom"\r\n'
    b"\r\n"
    b"--eom\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"no trailing newline after closing\r\n"
    b"--eom--"
)

CORPUS = [
    ("empty", b""),
    ("headers_only", b"Subject: no body\r\nFrom: a@example.com\r\n"),
    ("headers_and_blank", b"Subject: empty body\r\n\r\n"),
    ("simple_text", b"Subject: hi\r\nContent-Type: text/plain\r\n\r\nhello\r\n"),
    ("nested_mixed", NESTED_MIXED),
    ("encoded_headers", ENCODED_HEADERS),
    ("boundary_like_body", BOUNDARY_LIKE_BODY),
    ("boundary_prefix_in_body", BOUNDARY_PREFIX_IN_BODY),
    ("rfc2231_filename", RFC2231_FILENAME),
    ("unknown_charset_body", UNKNOWN_CHARSET_BODY),
    ("message_rfc822", MESSAGE_RFC822),
    ("quoted_boundary", QUOTED_BOUNDARY),
    ("qp_softbreak", QP_SOFTPREAK),
    ("long_header", LONG_HEADER),
    ("multipart_no_boundary", MULTIPART_NO_BOUNDARY),
    ("no_final_newline", NO_FINAL_NEWLINE),
]
