"""命令行演示：加密 / 整份解密 / 随机读取指定区间。

用法:
  python3 demo.py encrypt  明文 密文
  python3 demo.py decrypt  密文 明文
  python3 demo.py read     密文 偏移 长度 [--password 口令 | --key-hex 十六进制]
"""

import sys

import chunkcrypt as cc


def main() -> int:
    cmd = sys.argv[1]
    if cmd == "encrypt":
        key = cc.generate_key()
        meta = cc.encrypt_file(sys.argv[2], sys.argv[3], key=key)
        print("加密完成:", meta)
        print("主密钥(请妥善保管):", key.hex())
        return 0
    if cmd == "decrypt":
        key = bytes.fromhex(input("主密钥 hex: ").strip())
        cc.decrypt_file(sys.argv[2], sys.argv[3], key=key)
        print("解密完成 ->", sys.argv[3])
        return 0
    if cmd == "read":
        path, offset, length = sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
        key = password = None
        if "--key-hex" in sys.argv:
            key = bytes.fromhex(sys.argv[sys.argv.index("--key-hex") + 1])
        elif "--password" in sys.argv:
            password = sys.argv[sys.argv.index("--password") + 1]
        else:
            password = input("口令: ")
        with cc.ChunkReader(path, key=key, password=password) as r:
            sys.stdout.buffer.write(r.read_at(offset, length))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
