"""仅使用标准库的最小加密原语（教学/测试用途，非生产级）。

- 流加密：SHA256(key || nonce || counter) 生成密钥流，与明文异或。
  密钥流只依赖字节偏移，与块切分无关，因此块大小变化不影响密文兼容性。
- 完整性：每块一个 HMAC-SHA256 标签，覆盖文件头（作为关联数据）、块序号与密文。
"""

import hashlib
import hmac
import struct

TAG_SIZE = 32


def xor_keystream(key, nonce, data, offset=0):
    """从字节偏移 offset 处生成密钥流并与 data 异或（加解密同一函数）。"""
    result = bytearray()
    counter = offset // 32
    skip = offset % 32
    pos = 0
    while pos < len(data):
        stream = hashlib.sha256(key + nonce + struct.pack(">Q", counter)).digest()
        take = min(32 - skip, len(data) - pos)
        seg = stream[skip:skip + take]
        result += bytes(a ^ b for a, b in zip(data[pos:pos + take], seg))
        pos += take
        counter += 1
        skip = 0
    return bytes(result)


def block_tag(key, header_bytes, index, ciphertext):
    return hmac.new(
        key,
        b"EFMT-block\x00" + header_bytes + struct.pack(">I", index) + ciphertext,
        hashlib.sha256,
    ).digest()
