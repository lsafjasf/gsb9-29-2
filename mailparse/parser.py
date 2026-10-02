"""把导出的邮件报文拆成正文与附件。

功能：
- 解析折叠头部、参数化头部（含 RFC 2047 编码词、RFC 2231 参数续行）。
- 递归解析嵌套 multipart 与内嵌 message/rfc822，输出内容类型与层级路径。
- 还原 Content-Transfer-Encoding（base64 / quoted-printable / 7bit / 8bit / binary）。
- 字符集未知或解码失败时按明确策略降级，并在结果中标记。

所有内容均以 bytes 形式解析，避免过早字符集猜测。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from typing import Iterator, Optional, Union

# 声明字符集失败后的降级顺序：先无损兼容的 UTF-8，再 Latin-1（逐字节可逆，永不失败）。
FALLBACK_CHARSETS = ("utf-8", "latin-1")


@dataclass
class Part:
    """邮件中的一个 MIME 部分（或整封邮件的根节点）。"""

    path: str
    content_type: str
    headers: dict[str, str]
    params: dict[str, str]
    filename: Optional[str]
    disposition: Optional[str]
    charset: Optional[str]
    charset_used: Optional[str]
    charset_fallback: bool
    size: int
    sha256: str
    text: Optional[str] = None
    children: list["Part"] = field(default_factory=list)

    @property
    def is_container(self) -> bool:
        """multipart 或 message/rfc822 容器节点。"""
        return bool(self.children)

    @property
    def is_attachment(self) -> bool:
        if self.disposition == "attachment":
            return True
        return self.filename is not None and not self.children

    def walk(self) -> Iterator["Part"]:
        """按 MIME 顺序深度优先遍历所有部分。"""
        yield self
        for child in self.children:
            yield from child.walk()

    def to_dict(self, include_text: bool = False) -> dict:
        result = {
            "path": self.path,
            "content_type": self.content_type,
            "filename": self.filename,
            "disposition": self.disposition,
            "charset": self.charset,
            "charset_used": self.charset_used,
            "charset_fallback": self.charset_fallback,
            "size": self.size,
            "sha256": self.sha256,
            "params": self.params,
            "children": [child.to_dict(include_text=include_text) for child in self.children],
        }
        if include_text and self.text is not None:
            result["text"] = self.text
        return result


def decode_text(data: bytes, charset: Optional[str]) -> tuple[str, Optional[str], bool]:
    """按降级策略把字节解码为文本。

    返回 (文本, 实际使用字符集, 是否发生降级)。
    顺序：声明字符集 -> utf-8 -> latin-1。
    """
    candidates: list[str] = []
    if charset:
        candidates.append(charset)
    for fallback in FALLBACK_CHARSETS:
        if fallback not in candidates:
            candidates.append(fallback)

    for candidate in candidates:
        try:
            return data.decode(candidate), candidate, bool(charset) and candidate != charset
        except (LookupError, UnicodeDecodeError, ValueError):
            continue

    # latin-1 逐字节映射，理论上不会走到这里，保留兜底。
    return data.decode("latin-1", errors="replace"), "latin-1", True


def _headers(msg: Message) -> dict[str, str]:
    # policy.default 下 msg.items() 返回已解析的头部对象，
    # str() 会展开折叠并还原 RFC 2047 编码词；同名头部保留最后一个。
    result: dict[str, str] = {}
    for name, value in msg.items():
        result[name] = str(value)
    return result


def _raw_payload(msg: Message) -> bytes:
    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    raw = msg.get_payload()
    if isinstance(raw, str):
        try:
            return raw.encode("ascii", "surrogateescape")
        except UnicodeEncodeError:
            return raw.encode("utf-8", "surrogateescape")
    return b""


def _build(msg: Message, path: str) -> Part:
    content_type = msg.get_content_type()

    ct_header = msg["content-type"]
    params = dict(getattr(ct_header, "params", {}) or {})

    node = Part(
        path=path,
        content_type=content_type,
        headers=_headers(msg),
        params=params,
        filename=msg.get_filename(),
        disposition=msg.get_content_disposition(),
        charset=msg.get_content_charset(),
        charset_used=None,
        charset_fallback=False,
        size=0,
        sha256=hashlib.sha256(b"").hexdigest(),
    )

    if msg.is_multipart():
        for index, subpart in enumerate(msg.iter_parts(), start=1):
            node.children.append(_build(subpart, f"{path}.{index}"))
        return node

    if content_type == "message/rfc822" and isinstance(msg.get_payload(), Message):
        node.children.append(_build(msg.get_payload(), f"{path}.1"))
        return node

    data = _raw_payload(msg)
    node.size = len(data)
    node.sha256 = hashlib.sha256(data).hexdigest()

    if content_type.startswith("text/"):
        node.text, node.charset_used, node.charset_fallback = decode_text(data, node.charset)
    return node


def parse_message(data: Union[bytes, str]) -> Part:
    """解析整封邮件（bytes 或 str），返回根 Part。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, bytes):
        raise TypeError("data 必须是 bytes 或 str")
    msg = BytesParser(policy=policy.default).parsebytes(data)
    return _build(msg, "1")


def parse_file(path: Union[str, Path]) -> Part:
    """从 .eml 文件路径解析。"""
    return parse_message(Path(path).read_bytes())
