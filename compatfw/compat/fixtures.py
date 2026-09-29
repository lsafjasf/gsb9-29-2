"""版本样例文件与密钥的生成/加载。

每个版本都在 fixtures/v{1,2}/ 下保留：
- sample.enc       该版本程序写出的样例密文
- keys.json        key_version -> 主密钥（32 字节，base64）
- plaintext.bin    源明文（等价比对基准）
- manifest.json    版本、块大小、块数、nonce 等元数据

异常场景的派生文件放在 fixtures/scenarios/：
- key_missing_v{1,2}.enc   由缺失对应 key_version 的密钥环读取
- tampered_v{1,2}.enc      最后一个数据块密文翻转 1 bit（头 HMAC 仍有效）
- truncated_v{1,2}.enc     截掉最后一个块
- bigblock_v1.enc          v1 头块大小字段被改为 2048（头 HMAC 同步重签），
                           用于“块大小变化”场景
"""

import base64
import hashlib
import hmac
import json
import os
import struct
from dataclasses import dataclass
from typing import Dict

from . import codec
from .codec import V1, V2, MAGIC, MAC_LEN, Reader, Writer, KeyRing, _HDR_V1, _derive

FIXTURE_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "fixtures")

# 固定演示密钥（仅用于仓库内样例，可复现；非真实部署密钥）
FIXED_KEYS = {
    1: base64.b64decode("AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="),
    2: base64.b64decode("AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA="),
}
FIXED_NONCE_V2 = bytes.fromhex("0123456789abcdeffedcba9876543210")
SAMPLE_LEN = 5000  # 5000 字节：v1/1024 与 v2/2048 下都为多块 + 非满尾块


def sample_plaintext(length: int = SAMPLE_LEN) -> bytes:
    """确定性伪随机明文（LCG），保证跨运行一致。"""
    out = bytearray(length)
    state = 0x12345678
    for i in range(length):
        state = (1103515245 * state + 12345) & 0xFFFFFFFF
        out[i] = (state >> 16) & 0xFF
    return bytes(out)


@dataclass
class Bundle:
    version: int
    sample_path: str
    keys: KeyRing
    plaintext: bytes
    block_size: int
    block_count: int


def _paths(version: int) -> Dict[str, str]:
    d = os.path.join(FIXTURE_ROOT, f"v{version}")
    return {
        "dir": d,
        "sample": os.path.join(d, "sample.enc"),
        "keys": os.path.join(d, "keys.json"),
        "plaintext": os.path.join(d, "plaintext.bin"),
        "manifest": os.path.join(d, "manifest.json"),
    }


def generate(force: bool = False) -> None:
    """在 fixtures/ 下生成两个版本的样例文件、密钥与派生场景文件。"""
    pt = sample_plaintext()
    os.makedirs(os.path.join(FIXTURE_ROOT, "scenarios"), exist_ok=True)

    for version, block_size, kv in ((V1, 1024, 1), (V2, 2048, 2)):
        paths = _paths(version)
        os.makedirs(paths["dir"], exist_ok=True)
        key = FIXED_KEYS[kv]
        nonce = b"\x00" * 16 if version == V1 else FIXED_NONCE_V2
        blob = Writer(version, key, block_size, key_version=kv).write(pt, nonce=nonce)

        if force or not os.path.exists(paths["sample"]):
            with open(paths["sample"], "wb") as fh:
                fh.write(blob)
        ring = KeyRing({kv: key})
        ring.save_json(paths["keys"])
        with open(paths["plaintext"], "wb") as fh:
            fh.write(pt)
        block_count = (len(pt) + block_size - 1) // block_size
        with open(paths["manifest"], "w", encoding="utf-8") as fh:
            json.dump({
                "version": version,
                "key_version": kv,
                "block_size": block_size,
                "block_count": block_count,
                "plaintext_len": len(pt),
                "plaintext_sha256": hashlib.sha256(pt).hexdigest(),
                "nonce_hex": nonce.hex(),
                "sample_sha256": hashlib.sha256(blob).hexdigest(),
            }, fh, indent=2, sort_keys=True)

        sdir = os.path.join(FIXTURE_ROOT, "scenarios")
        stem = os.path.join(sdir, f"{{name}}_v{version}.enc")

        # 1) 密钥版本缺失：文件不变，读取时用不含该 kv 的密钥环（矩阵中构造）。
        with open(stem.format(name="key_missing"), "wb") as fh:
            fh.write(blob)

        # 2) 块被篡改：翻转最后一个块密文中的 1 bit（头 HMAC 不受影响）。
        tampered = bytearray(blob)
        tampered[-1 - MAC_LEN - 3] ^= 0x01
        with open(stem.format(name="tampered"), "wb") as fh:
            fh.write(bytes(tampered))

        # 3) 截断：去掉最后一个完整块记录。
        last_len = len(pt) - block_size * (block_count - 1)
        last_record = 16 + last_len + MAC_LEN
        with open(stem.format(name="truncated"), "wb") as fh:
            fh.write(blob[:len(blob) - last_record])

    # 4) 块大小变化：取 v1 文件，把头中 block_size 改为 2048 并重签头 HMAC。
    v1_paths = _paths(V1)
    with open(v1_paths["sample"], "rb") as fh:
        v1_blob = bytearray(fh.read())
    magic, ver, kv, old_bs, bc = _HDR_V1.unpack(bytes(v1_blob[:20]))
    new_header = _HDR_V1.pack(MAGIC, V1, kv, 2048, bc)
    v1_blob[:20] = new_header
    hdr_mac_key, _, _ = _derive(V1, kv, FIXED_KEYS[kv])
    v1_blob[20:20 + MAC_LEN] = hmac.new(
        hdr_mac_key, new_header, hashlib.sha256
    ).digest()
    with open(os.path.join(FIXTURE_ROOT, "scenarios", "bigblock_v1.enc"), "wb") as fh:
        fh.write(bytes(v1_blob))


def load_bundle(version: int) -> Bundle:
    paths = _paths(version)
    if not os.path.exists(paths["sample"]):
        generate()
    with open(paths["manifest"], "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    with open(paths["plaintext"], "rb") as fh:
        pt = fh.read()
    return Bundle(
        version=version,
        sample_path=paths["sample"],
        keys=KeyRing.load_json(paths["keys"]),
        plaintext=pt,
        block_size=manifest["block_size"],
        block_count=manifest["block_count"],
    )


def load_blob(version: int) -> bytes:
    with open(load_bundle(version).sample_path, "rb") as fh:
        return fh.read()


def scenario_path(name: str, version: int) -> str:
    return os.path.join(FIXTURE_ROOT, "scenarios", f"{name}_v{version}.enc")


def load_scenario(name: str, version: int) -> bytes:
    with open(scenario_path(name, version), "rb") as fh:
        return fh.read()


def combined_ring() -> KeyRing:
    """同时包含 kv=1 与 kv=2 的密钥环（正常读取方使用）。"""
    return KeyRing(dict(FIXED_KEYS))
