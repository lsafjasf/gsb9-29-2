"""三个故意写错的读取器实现，用于框架自测：
框架必须把它们判为失败，否则说明框架本身判不出错误实现。
"""


from . import format_v1, format_v2
from .crypto import TAG_SIZE, xor_keystream
from .errors import Reason, RejectError
from .framework import ReadOk


def partial_reader(blob, keyring):
    """错误1：块校验失败时，把已解出的前缀当作成功结果返回（部分内容）。"""
    header_size = format_v2.HEADER.size
    (magic, version, key_version, block_size, block_count,
     data_len, flags, nonce) = format_v2.HEADER.unpack_from(blob)
    key = keyring.get(key_version)
    if key is None:
        raise RejectError(Reason.KEY_UNAVAILABLE, "no key")
    plain = []
    off = header_size
    for i in range(block_count):
        clen = min(block_size, data_len - i * block_size)
        ct = blob[off:off + clen]
        tag = blob[off + clen:off + clen + TAG_SIZE]
        from .crypto import block_tag
        import hmac as _h
        if not _h.compare_digest(tag, block_tag(key, blob[:header_size], i, ct)):
            return ReadOk(b"".join(plain))  # 错误：静默返回部分内容
        plain.append(xor_keystream(key, nonce, ct, offset=i * block_size))
        off += clen + TAG_SIZE
    return ReadOk(b"".join(plain)[:data_len])


def no_integrity_reader(blob, keyring):
    """错误2：完全跳过 HMAC 校验，被篡改的块也照常解密返回。"""
    (magic, version, key_version, block_size, block_count,
     data_len, flags, nonce) = format_v2.HEADER.unpack_from(blob)
    key = keyring.get(key_version)
    if key is None:
        raise RejectError(Reason.KEY_UNAVAILABLE, "no key")
    plain = []
    off = format_v2.HEADER.size
    for i in range(block_count):
        clen = min(block_size, data_len - i * block_size)
        ct = blob[off:off + clen]
        plain.append(xor_keystream(key, nonce, ct, offset=i * block_size))
        off += clen + TAG_SIZE
    return ReadOk(b"".join(plain)[:data_len])


def silent_version_reader(blob, keyring):
    """错误3：不检查版本号，把任何文件都按 v1 布局解析。"""
    (magic, version, key_version, block_size, block_count,
     data_len, nonce) = format_v1.HEADER.unpack_from(blob)
    key = keyring.get(key_version)
    if key is None:
        raise RejectError(Reason.KEY_UNAVAILABLE, "no key")
    plain = []
    off = format_v1.HEADER.size
    for i in range(block_count):
        clen = min(block_size, data_len - i * block_size)
        ct = blob[off:off + clen]
        plain.append(xor_keystream(key, nonce, ct, offset=i * block_size))
        off += clen + TAG_SIZE
    return ReadOk(b"".join(plain)[:data_len])


BUGGY_READERS = {
    "错误实现A·篡改后返回部分内容": (2, partial_reader),
    "错误实现B·跳过完整性校验": (2, no_integrity_reader),
    "错误实现C·不检查版本号": (1, silent_version_reader),
}
