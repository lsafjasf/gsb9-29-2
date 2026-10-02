#!/usr/bin/env python3
"""chunkcrypt 攻击实验：整块替换 / 块重排 / 块截断 / 重放 / 拼接 / DoS。

只读分析，不修改被测代码。被测实现通过 --src 指定目录后动态导入。

用法:
    python3 attack_experiments.py [--src /home/administrator/gsb/uid56/B]

每个实验打印 [检出] / [静默接受] / [行为] 及观察到的异常类型或返回数据。
退出码恒为 0（实验是观察性的，结论由人/文档解读）。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import struct
import sys
import tempfile
import time

CHUNK = 64                 # 实验用小块，便于构造
DATA = bytes(range(256))   # 256 字节 = 4 个满块


def load_chunkcrypt(src_dir: str):
    path = os.path.join(src_dir, "chunkcrypt.py")
    spec = importlib.util.spec_from_file_location("chunkcrypt", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def layout(cc):
    """从被测模块常量推导各区域偏移（与 chunkcrypt.py L51-53, L237 一致）。"""
    header = cc.HEADER_SIZE                       # 96
    index = lambda n: header                      # 索引区起点（紧随头部）
    data = lambda n: header + n * cc.MAC_SIZE + cc.INDEX_MAC_SIZE  # 数据区起点
    return header, index, data


def encrypt(cc, key, path, data=DATA, chunk_size=CHUNK, **kw):
    with open(path, "wb") as f:
        f.write(data)
    cc.encrypt_file(path, path + ".enc", key=key, chunk_size=chunk_size, **kw)
    return path + ".enc"


def try_open(cc, path, key=None, password=None):
    """尝试打开并全量读取，返回 (状态, 详情)。"""
    try:
        with cc.ChunkReader(path, key=key, password=password) as r:
            out = r.read_all()
        return "OK", out
    except Exception as e:                        # 观察异常类型即可
        return type(e).__name__, str(e)


def show(name, expect, status, detail):
    tag = {"OK": "[静默接受]"}.get(status, "[检出]")
    if status == "OK" and isinstance(detail, bytes):
        detail = f"读出 {len(detail)} 字节: {detail[:24].hex()}{'...' if len(detail) > 24 else ''}"
    print(f"  {tag} {name}\n      -> {status}: {detail}\n      （预期: {expect}）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="/home/administrator/gsb/uid56/B",
                    help="被测 chunkcrypt.py 所在目录")
    args = ap.parse_args()
    cc = load_chunkcrypt(args.src)
    print(f"被测实现: {os.path.join(args.src, 'chunkcrypt.py')}")
    HEADER, INDEX, DATA_OFF = layout(cc)
    print(f"布局: HEADER_SIZE={HEADER} MAC_SIZE={cc.MAC_SIZE} "
          f"data_offset(4块)={DATA_OFF(4)}\n")

    key = cc.generate_key()
    tmp = tempfile.mkdtemp(prefix="chunkcrypt-atk-")
    print(f"实验目录: {tmp}\n")

    def fresh(name="e"):
        p = os.path.join(tmp, name)
        enc = encrypt(cc, key, p)
        with open(enc, "rb") as f:
            return bytearray(f.read())

    def write(buf, name="atk.enc"):
        p = os.path.join(tmp, name)
        with open(p, "wb") as f:
            f.write(buf)
        return p

    d0 = DATA_OFF(4)  # 数据区起点（4 块时）

    print("== E0 基线：正常加密往返 ==")
    enc0 = encrypt(cc, key, os.path.join(tmp, "base"))
    st, dt = try_open(cc, enc0, key=key)
    assert st == "OK" and dt == DATA
    print(f"  [行为] 正常文件 read_all 返回原文 {len(dt)} 字节，与明文一致\n")

    print("== E1 单比特篡改（块内 1 bit） ==")
    buf = fresh("e1"); buf[d0 + 10] ^= 0x01
    st, dt = try_open(cc, write(buf, "e1.enc"), key=key)
    show("翻转 chunk0 第 10 字节的一个 bit", "ChunkTamperedError(0)", st, dt)
    # 篡改局部性：未篡改块仍可读
    p = write(buf, "e1b.enc")
    try:
        with cc.ChunkReader(p, key=key) as r:
            ok = r.read_at(2 * CHUNK, CHUNK) == DATA[2 * CHUNK:3 * CHUNK]
        print(f"  [行为] 篡改局部性: chunk2 仍可正常读出 = {ok}\n")
    except Exception as e:
        print(f"  [行为] 篡改局部性: 读 chunk2 也失败 {type(e).__name__}: {e}\n")

    print("== E2 整块替换（随机字节填充 chunk1） ==")
    buf = fresh("e2"); buf[d0 + CHUNK: d0 + 2 * CHUNK] = os.urandom(CHUNK)
    st, dt = try_open(cc, write(buf, "e2.enc"), key=key)
    show("chunk1 整块换成随机字节", "ChunkTamperedError(1)", st, dt)

    print("== E3 块重排（仅交换数据区 chunk0<->chunk1） ==")
    buf = fresh("e3")
    c0 = bytes(buf[d0: d0 + CHUNK]); c1 = bytes(buf[d0 + CHUNK: d0 + 2 * CHUNK])
    buf[d0: d0 + CHUNK] = c1; buf[d0 + CHUNK: d0 + 2 * CHUNK] = c0
    st, dt = try_open(cc, write(buf, "e3.enc"), key=key)
    show("交换 chunk0/chunk1 密文", "ChunkTamperedError（块号绑定）", st, dt)

    print("== E4 块重排（数据 + 索引项一起交换） ==")
    buf = fresh("e4")
    i0 = bytes(buf[INDEX(4): INDEX(4) + 32]); i1 = bytes(buf[INDEX(4) + 32: INDEX(4) + 64])
    buf[INDEX(4): INDEX(4) + 32] = i1; buf[INDEX(4) + 32: INDEX(4) + 64] = i0
    buf[d0: d0 + CHUNK] = c1; buf[d0 + CHUNK: d0 + 2 * CHUNK] = c0
    st, dt = try_open(cc, write(buf, "e4.enc"), key=key)
    show("chunk 密文与索引项同步交换", "IntegrityError（index_mac 失配）", st, dt)

    print("== E5 块截断（删掉最后一块的 32 字节） ==")
    buf = fresh("e5"); del buf[-32:]
    st, dt = try_open(cc, write(buf, "e5.enc"), key=key)
    show("文件尾部截短 32 字节", "ChunkMissingError（打开即拒绝）", st, dt)

    print("== E5b 块截断（从中间挖掉一整块，保持总长度不变） ==")
    buf = fresh("e5b")
    del buf[d0 + CHUNK: d0 + 2 * CHUNK]          # 挖掉 chunk1
    buf += b"\x00" * CHUNK                        # 末尾补齐，长度不变
    st, dt = try_open(cc, write(buf, "e5b.enc"), key=key)
    show("中间删块+补零（后续块错位）", "ChunkTamperedError（错位处失配）", st, dt)

    print("== E6 尾部追加（附加 32 字节垃圾） ==")
    buf = fresh("e6"); buf += os.urandom(32)
    st, dt = try_open(cc, write(buf, "e6.enc"), key=key)
    show("文件末尾追加 32 字节", "IntegrityError（trailing garbage）", st, dt)

    print("== E7 跨文件整块重放（同密钥、同块号，数据+索引项一起搬） ==")
    encB = encrypt(cc, key, os.path.join(tmp, "other"),
                   data=b"\xAA" * 256)            # 同 key 的另一文件
    with open(encB, "rb") as f:
        bbuf = f.read()
    buf = fresh("e7")
    buf[d0 + CHUNK: d0 + 2 * CHUNK] = bbuf[d0 + CHUNK: d0 + 2 * CHUNK]   # B 的 chunk1
    buf[INDEX(4) + 32: INDEX(4) + 64] = bbuf[INDEX(4) + 32: INDEX(4) + 64]  # B 的索引项1
    st, dt = try_open(cc, write(buf, "e7.enc"), key=key)
    show("把文件B的 chunk1(密文+MAC) 移植进文件A", "IntegrityError（index_mac 失配）", st, dt)

    print("== E8 跨文件拼接（A 的头部 + B 的索引与数据，同密钥同长度） ==")
    buf = fresh("e8")
    buf[HEADER:] = bbuf[HEADER:]                  # 保留 A 头，换上 B 的索引区+数据区
    st, dt = try_open(cc, write(buf, "e8.enc"), key=key)
    show("A头+B身（header_mac/index_mac/各chunk MAC 全部各自有效）",
         "若格式把三段绑定则应拒绝；实际预期：静默接受且输出乱码", st, dt)
    if st == "OK":
        print(f"  [行为] 输出 == A 原文? {dt == DATA}；== B 原文? {dt == b'\\xAA' * 256}"
              "（两者皆否 = 无声损坏）\n")

    print("== E9 整文件回滚重放（旧版本整体替换新版本） ==")
    pV = os.path.join(tmp, "vault")
    encV1 = encrypt(cc, key, pV, data=b"OLD-SECRET-v1".ljust(256, b"\x00"))
    with open(encV1, "rb") as f:
        v1 = f.read()
    encV2 = encrypt(cc, key, pV, data=b"NEW-SECRET-v2".ljust(256, b"\x00"))
    with open(encV2, "wb") as f:                  # 攻击者把落盘文件回滚成 v1
        f.write(v1)
    st, dt = try_open(cc, encV2, key=key)
    show("用旧版本密文整体覆盖当前文件", "格式无版本/新鲜度机制 -> 静默接受旧明文", st, dt)
    if st == "OK":
        print(f"  [行为] 读出的明文以 {dt[:13]!r} 开头 —— 旧版本被无声接受\n")

    print("== E10 部分回滚（新头部 + 旧索引与旧数据） ==")
    encV3 = encrypt(cc, key, pV, data=b"THIRD-v3".ljust(256, b"\x00"))
    with open(encV3, "rb") as f:
        v3 = bytearray(f.read())
    v3_orig = bytes(v3)                           # E12 要用未变异的 v3
    v3[HEADER:] = v1[HEADER:]                     # 新头 + v1 的索引与数据
    st, dt = try_open(cc, write(v3, "e10.enc"), key=key)
    show("保留 v3 头部、回滚索引+数据到 v1", "同 E8：三段无绑定 -> 静默接受乱码", st, dt)

    print("== E11 未认证的 kdf_iterations（打开期 CPU-DoS） ==")
    pP = os.path.join(tmp, "pw")
    with open(pP, "wb") as f:
        f.write(DATA)
    cc.encrypt_file(pP, pP + ".enc", password="hunter2", chunk_size=CHUNK,
                    kdf_iterations=200_000)
    with open(pP + ".enc", "rb") as f:
        buf = bytearray(f.read())
    # 头部字段 kdf_iterations 位于 offset 28..32（_HEADER_STRUCT <8sIQQI>）
    it_off = struct.calcsize("<8sIQQ")
    orig_it = struct.unpack_from("<I", buf, it_off)[0]
    evil_it = 30_000_000
    struct.pack_into("<I", buf, it_off, evil_it)  # 不重算 header_mac（攻击者也没有密钥）
    evil = write(buf, "e11.enc")
    t0 = time.perf_counter()
    st, dt = try_open(cc, evil, password="hunter2")
    dt_s = time.perf_counter() - t0
    print(f"  [行为] header 中 kdf_iterations {orig_it} -> {evil_it}（header_mac 必然失配）")
    print(f"  [行为] 打开耗时 {dt_s:.2f}s 后才抛 {st} —— PBKDF2 在 header_mac 校验之前执行")
    print(f"      （对照: 正常 200k 轮约 0.03s；攻击者可写到 2^32-1 轮）\n")

    print("== E12 同文件旧版本单块重放（旧 chunk + 旧索引项） ==")
    buf = bytearray(v3_orig)                       # 未变异的 v3 全量
    buf[d0: d0 + CHUNK] = v1[d0: d0 + CHUNK]       # chunk0 回滚到 v1
    buf[INDEX(4): INDEX(4) + 32] = v1[INDEX(4): INDEX(4) + 32]  # 索引项0 一起回滚
    st, dt = try_open(cc, write(buf, "e12.enc"), key=key)
    show("v3 中把 chunk0(密文+MAC) 回滚到 v1", "IntegrityError（index_mac 失配）", st, dt)

    print("实验完成。详细结论见 ANALYSIS.md。")


if __name__ == "__main__":
    main()
