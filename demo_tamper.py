"""篡改检出演示：对同一报文施加各类篡改，打印检出的错误类型。

运行方式：python3 demo_tamper.py
"""

import aead

KEY = aead.generate_key()
AAD = b"header:tenant=uid53;ctx=demo"
PLAINTEXT = b"hello, authenticated encryption!"


def flip_bit(data: bytes, offset: int) -> bytes:
    buf = bytearray(data)
    buf[offset] ^= 0x01
    return bytes(buf)


def main() -> None:
    packet = aead.seal(KEY, PLAINTEXT, AAD)
    aad_len = len(AAD)
    ct_offset = 1 + 4 + aad_len + 8 + aead.NONCE_SIZE
    ct_end = len(packet) - aead.TAG_SIZE

    cases = [
        ("原始报文（对照组）", packet),
        ("密文改动（单比特翻转）", flip_bit(packet, ct_offset + 3)),
        ("头部改动（AAD 单比特翻转）", flip_bit(packet, 1 + 4 + 1)),
        ("头部改动（nonce 翻转）", flip_bit(packet, ct_offset - 1)),
        ("头部改动（版本号翻转）", flip_bit(packet, 0)),
        ("标签改动（末字节翻转）", flip_bit(packet, len(packet) - 1)),
        (
            "字段重排（AAD 与密文区对调）",
            packet[: 1 + 4]
            + packet[1 + 4 + aad_len : ct_end]
            + packet[1 + 4 : 1 + 4 + aad_len]
            + packet[-aead.TAG_SIZE :],
        ),
        (
            "字段重排（密文两块对调）",
            packet[:ct_offset]
            + packet[ct_offset + 5 : ct_end]
            + packet[ct_offset : ct_offset + 5]
            + packet[ct_end:],
        ),
        ("截断（去掉末尾 16 字节）", packet[:-16]),
        ("截断（只剩一半）", packet[: len(packet) // 2]),
        ("尾部追加垃圾字节", packet + b"\x00"),
        ("错误密钥解封（对照）", packet),
    ]

    print(f"{'篡改用例':<28} {'结果':<6} 检出类型 / 说明")
    print("-" * 78)
    for i, (name, blob) in enumerate(cases):
        key = aead.generate_key() if i == len(cases) - 1 else KEY
        try:
            plaintext, aad = aead.open(key, blob)
        except aead.AEADError as exc:
            print(f"{name:<28} {'拒绝':<6} {type(exc).__name__}: {exc}")
        else:
            print(f"{name:<28} {'接受':<6} 明文={plaintext!r} AAD={aad!r}")


if __name__ == "__main__":
    main()
