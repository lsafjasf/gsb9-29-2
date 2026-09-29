"""为每个格式版本生成并保留样例文件与密钥（完全确定性，可重复生成）。

目录结构：
    samples/
      keys.json                    密钥环 {"1": hex, "2": hex}（故意不含 kv9）
      v1/payload.bin               期望明文（判等基准）
      v1/sample.enc                正常 v1 文件（kv1）
      v1/sample.tampered.enc       第 1 块密文被翻转 1 字节
      v1/sample.missingkey.enc     用 kv9 加密的正常文件（密钥环中没有 kv9）
      v2/... 同上（kv2）
"""

import hashlib
import json
import os
import struct

from . import format_v1, format_v2

MISSING_KEY_VERSION = 9
FORMATS = {1: format_v1, 2: format_v2}


def key_for(key_version):
    return hashlib.sha256(b"efmt-sample-key-%d" % key_version).digest()


def payload_for(version):
    """确定性伪随机明文，9000+version 字节（保证 v1/v2 都有多个块）。"""
    n = 9000 + version
    seed = hashlib.sha256(b"efmt-payload-v%d" % version).digest()
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(seed + struct.pack(">I", counter)).digest()
        counter += 1
    return bytes(out[:n])


def tamper(blob, version):
    """翻转第 1 块（从 0 计）密文中的一个字节，模拟块被篡改。"""
    fmt = FORMATS[version]
    header_size = fmt.HEADER.size
    block0_len = min(fmt.BLOCK_SIZE, 9000 + version)
    pos = header_size + block0_len + 32 + 5  # 块1密文第 5 字节
    buf = bytearray(blob)
    buf[pos] ^= 0xFF
    return bytes(buf)


def build_samples(root):
    """生成全部样例；返回样例根目录路径。"""
    keyring = {}
    for version, fmt in FORMATS.items():
        keyring[str(fmt.KEY_VERSION)] = key_for(fmt.KEY_VERSION).hex()
        vdir = os.path.join(root, "v%d" % version)
        os.makedirs(vdir, exist_ok=True)
        payload = payload_for(version)
        _write(os.path.join(vdir, "payload.bin"), payload)
        normal = fmt.write_file(payload, key_for(fmt.KEY_VERSION))
        _write(os.path.join(vdir, "sample.enc"), normal)
        _write(os.path.join(vdir, "sample.tampered.enc"), tamper(normal, version))
        missing = fmt.write_file(payload, key_for(MISSING_KEY_VERSION),
                                 key_version=MISSING_KEY_VERSION)
        _write(os.path.join(vdir, "sample.missingkey.enc"), missing)
    with open(os.path.join(root, "keys.json"), "w", encoding="utf-8") as f:
        json.dump(keyring, f, indent=2, sort_keys=True)
    return root


def _write(path, data):
    with open(path, "wb") as f:
        f.write(data)


def load_keyring(root):
    with open(os.path.join(root, "keys.json"), encoding="utf-8") as f:
        return {int(k): bytes.fromhex(v) for k, v in json.load(f).items()}


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def load_cases(root):
    """从保留的样例文件构建测试用例（含跨版本写入回读用例）。"""
    from .framework import Case

    keyring = load_keyring(root)
    cases = []
    for version in FORMATS:
        vdir = os.path.join(root, "v%d" % version)
        payload = _read(os.path.join(vdir, "payload.bin"))
        cases.append(Case(
            name="v%d 样例·正常" % version, group="存档样例读取",
            blob=_read(os.path.join(vdir, "sample.enc")), keyring=keyring,
            file_version=version, scenario="normal", expected_plaintext=payload,
            basis="同版本应等价读取；新版读旧版应转换；旧版读新版应拒绝"))
        cases.append(Case(
            name="v%d 样例·块被篡改" % version, group="存档样例读取",
            blob=_read(os.path.join(vdir, "sample.tampered.enc")), keyring=keyring,
            file_version=version, scenario="tampered", expected_plaintext=payload,
            basis="块 HMAC 校验必须失败并明确拒绝，不得返回部分明文"))
        cases.append(Case(
            name="v%d 样例·密钥版本缺失" % version, group="存档样例读取",
            blob=_read(os.path.join(vdir, "sample.missingkey.enc")), keyring=keyring,
            file_version=version, scenario="missingkey", expected_plaintext=payload,
            basis="文件要求 kv%d，密钥环中只有 kv1/kv2，应明确拒绝"
                  % MISSING_KEY_VERSION))
    # 跨版本写入回读：每个版本的写入器写出的文件，交给所有读取器读
    for wversion, fmt in FORMATS.items():
        payload = payload_for(wversion)
        blob = fmt.write_file(payload, key_for(fmt.KEY_VERSION))
        cases.append(Case(
            name="v%d 写入 → 回读" % wversion, group="跨版本写入回读",
            blob=blob, keyring=keyring,
            file_version=wversion, scenario="roundtrip", expected_plaintext=payload,
            basis="写入-读取往返，内容必须逐字节等价"))
    # v2 写入器降级写 v1 格式
    payload = payload_for(2)
    blob = format_v2.write_file(payload, key_for(format_v1.KEY_VERSION),
                                target_version=1)
    cases.append(Case(
        name="v2 写入器降级写 v1 → 回读", group="跨版本写入回读",
        blob=blob, keyring=keyring,
        file_version=1, scenario="roundtrip", expected_plaintext=payload,
        basis="降级写出的 v1 文件，v1 应等价读取，v2 应转换读取"))
    return cases
