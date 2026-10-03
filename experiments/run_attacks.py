#!/usr/bin/env python3
"""chunkcrypt 攻击实验（仅标准库；不导入、不修改任何第三方代码以外的内容）。

被测代码以字节形式从 ../target/chunkcrypt.py 原样加载（只读，不修改）。
运行:  python3 experiments/run_attacks.py
输出:  逐条实验结果到 stdout，并把所有构造的篡改样本写入 experiments/tmp/。
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "target" / "chunkcrypt.py"
WORK = ROOT / "experiments" / "tmp"

spec = importlib.util.spec_from_file_location("chunkcrypt_under_test", TARGET)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)  # type: ignore[union-attr]

# 固定密钥与块大小，保证实验可复现；CS 取 64B 以便快速构造多块文件。
KEY = bytes(range(32))
CS = 64
WORK.mkdir(parents=True, exist_ok=True)


def blocks(content: bytes, size: int = CS) -> bytes:
    return b"".join(bytes([c]) * size for c in content)


def write_enc(name: str, plaintext: bytes, key=KEY, chunk_size=CS,
              password=None) -> bytes:
    path = WORK / name
    with open(path, "wb") as dst:
        cc.encrypt_stream(io.BytesIO(plaintext), dst, key=key,
                          password=password, chunk_size=chunk_size)
    return Path(path).read_bytes()


def save(name: str, data: bytes) -> str:
    (WORK / name).write_bytes(data)
    return str(WORK / name)


def open_reader(blob: bytes, name: str, key=KEY):
    path = save(name, blob)
    return cc.ChunkReader(path, key=key), path


def exc_name(exc: BaseException) -> str:
    return type(exc).__name__ + (
        f"({getattr(exc, 'chunk_index', '')})"
        if isinstance(exc, (cc.ChunkTamperedError, cc.ChunkMissingError))
        else "")


results: list[tuple[str, str, str, str]] = []  # id, 期望, 实际, 判定


def record(eid: str, expect: str, got: str, detected: bool) -> None:
    verdict = "DETECTED   " if detected else "SILENT-ACCEPT"
    results.append((eid, expect, got, verdict))
    print(f"[{verdict}] {eid:4s} 期望={expect:22s} 实际={got}")


def try_open(blob: bytes, name: str):
    """返回 (reader_or_None, 异常描述)。"""
    try:
        reader, path = open_reader(blob, name)
        return reader, "open-ok"
    except BaseException as exc:  # noqa: BLE001 - 实验要观察所有异常类型
        return None, exc_name(exc)


def try_read(blob: bytes, name: str, offset: int, length: int):
    reader, open_got = try_open(blob, name)
    if reader is None:
        return None, f"打开阶段:{open_got}"
    try:
        return reader.read_at(offset, length), "read-ok"
    except BaseException as exc:  # noqa: BLE001
        return None, f"读取阶段:{exc_name(exc)}"
    finally:
        reader.close()


# ---------------------------------------------------------------- fixtures
plainA = blocks(b"ABCDE")                       # 5 个完整块, 320B
plainB = blocks(b"VWXYZ")                       # 另一文件，同密钥同尺寸
plainT = blocks(b"ABCDE") + b"T" * 13           # 5 块 + 短尾块, 333B
A = write_enc("A.enc", plainA)
B = write_enc("B.enc", plainB)
T = write_enc("tail.enc", plainT)
PW = write_enc("pw.enc", plainA, key=None, chunk_size=CS,
               password="correct horse battery staple")  # 口令加密样本
H = cc.HEADER_SIZE
IDX = 32
D0 = H + 5 * IDX + IDX          # 5 块文件的数据区起点（头部+索引+index_mac）

print(f"被测文件: {TARGET}")
print(f"HEADER_SIZE={H}（README 文字称 120B；代码常量为 96B，见 chunkcrypt.py:52）")
print(f"固定密钥={KEY.hex()} chunk_size={CS} 文件A={len(plainA)}B / "
      f"5 块, 文件B 同尺寸不同内容；另含尾块文件 {len(plainT)}B")
print("=" * 78)

# --------------------------------------------------------- E0 基线（无攻击）
r, got = try_open(A, "base_A.enc")
ok = r is not None and r.read_all() == plainA
r and r.close()
record("E0", "正常往返", got + (",read_all 一致" if ok else ",数据不一致"), ok)

# ===================================================== E1 整块替换 / 字节篡改
# E1a 翻转第 2 块密文 1 字节（不碰索引）
m = bytearray(A)
m[D0 + 2 * CS + 0] ^= 0xFF           # 数据区第 2 块第 1 字节
m = bytes(m)
r, got = try_open(m, "e1a_flip.enc")  # 打开应成功（逐块校验是惰性的）
silent_open = r is not None
if r:
    try:
        good = r.read_at(0, 8)        # 未篡改块仍可读
        r.read_at(2 * CS, 8)
        read_got = "篡改块被静默接受!"
        detected = False
    except BaseException as exc:     # noqa: BLE001
        read_got = exc_name(exc)
        detected = (isinstance(exc, cc.ChunkTamperedError)
                    and exc.chunk_index == 2)
    finally:
        r.close()
    got = f"open-ok;块0={good!r};块2->{read_got}"
else:
    detected = True
record("E1a", "打开通过+读块2报 Tampered(2)", got, detected)

# E1b 用文件 B 第 2 块的“合法密文”整块替换文件 A 第 2 块（跨文件整块替换）
m = A[:D0 + 2 * CS] + B[D0 + 2 * CS:D0 + 3 * CS] + A[D0 + 3 * CS:]
data, got = try_read(m, "e1b_foreign_block.enc", 2 * CS, 8)
record("E1b", "读块2报 Tampered(2)", got,
       isinstance(got, str) and "ChunkTamperedError(2)" in got)

# ============================================================ E2 块重放/重排
# E2a 同文件内交换数据区第 1、2 块（文件长度不变，不碰索引）
d0 = H + 5 * 32 + 32
m = bytearray(A)
m[d0 + 1 * CS:d0 + 3 * CS] = (
    A[d0 + 2 * CS:d0 + 3 * CS] + A[d0 + 1 * CS:d0 + 2 * CS])
data, got1 = try_read(bytes(m), "e2a_swap_data.enc", 0, 8)
data2, got2 = try_read(bytes(m), "e2a_swap_data.enc", 1 * CS, 8)
data3, got3 = try_read(bytes(m), "e2a_swap_data.enc", 2 * CS, 8)
det = ("read-ok" == got1 and "ChunkTamperedError(1)" in got2
       and "ChunkTamperedError(2)" in got3)
record("E2a", "块0正常;块1/2报 Tampered",
       f"块0:{got1};块1:{got2};块2:{got3}", det)

# E2b 只交换索引区第 1、2 项（index_mac 不会重算）
m = bytearray(A)
a1, a2 = H + 1 * 32, H + 2 * 32
m[a1:a1 + 32], m[a2:a2 + 32] = m[a2:a2 + 32], m[a1:a1 + 32]
r, got = try_open(bytes(m), "e2b_swap_index.enc")
r and r.close()
record("E2b", "打开报 IntegrityError(index)", got,
       isinstance(got, str) and "IntegrityError" in got)

# E2c 跨文件“重放载荷”：B 的(已认证)头部 + A 的索引区+数据区
#     两文件同密钥、同 chunk_size、同 total_size、同 chunk_count。
m = B[:H] + A[H:]
r, got = try_open(m, "e2c_graft.enc")
served = None
if r is not None:
    served = r.read_all()
    r.close()
silent = (served is not None and served != plainA and served != plainB)
record("E2c", "★预期静默: 打开/索引/逐块MAC全过，返回错乱明文",
       got + (f";read_all {len(served)}B 且≠任一合法明文" if silent else ""),
       not silent)  # “检出”列：静默 => False

# E2d 只把 B 索引中的 1 个 MAC 项挪进 A（单条索引跨文件重放）
m = bytearray(A)
m[H + 3 * 32:H + 4 * 32] = B[H + 3 * 32:H + 4 * 32]
r, got = try_open(bytes(m), "e2d_foreign_mac.enc")
r and r.close()
record("E2d", "打开报 IntegrityError(index)", got,
       isinstance(got, str) and "IntegrityError" in got)

# E2e 旧版本整文件重放：v1 与 v2 同尺寸不同内容，用 v1 整个替换 v2
v1 = write_enc("v1.enc", blocks(b"AAAAA"))
v2 = write_enc("v2.enc", blocks(b"ZZZZZ"))
r, got = try_open(v1, "v1_replayed_as_v2.enc")  # 攻击者把磁盘上的 v2 换回 v1
served = r.read_all() if r else None
r and r.close()
silent = served == blocks(b"AAAAA")
record("E2e", "★预期静默: 旧文件被当作当前文件接受", got +
       ";读回 v1 旧数据" if silent else "", not silent)

# ================================================================ E3 截断
# E3a 砍掉最后半个块（头部 chunk_count 仍=5）
m = A[:-CS // 2]
r, got = try_open(m, "e3a_halfcut.enc")
r and r.close()
record("E3a", "打开报 ChunkMissingError(4)", got,
       isinstance(got, str) and "ChunkMissingError(4)" in got)

# E3b 保留长度但从中间挖掉一整块（末块前移，长度差 CS）
m = A[:d0 + 3 * CS] + A[d0 + 4 * CS:]   # 删掉第 3 块, 文件少 64B
r, got = try_open(m, "e3b_mid_delete.enc")
if r:
    try:
        r.read_at(3 * CS, 8)
        got += ";块3被静默接受!"
        det = False
    except BaseException as exc:       # noqa: BLE001
        got += ";块3->" + exc_name(exc)
        det = isinstance(exc, (cc.ChunkTamperedError, cc.ChunkMissingError))
    r.close()
else:
    det = "ChunkMissingError" in got
record("E3b", "打开或读块3时报缺失/篡改", got, det)

# E3c 从中间挖 1 块并用下一块内容补齐到原长度（README 宣称场景）
m = A[:d0 + 2 * CS] + A[d0 + 3 * CS:] + A[-CS:]  # 长度恢复, 后续错位/重复
data, got = try_read(m, "e3c_shift.enc", 3 * CS, 8)
record("E3c", "读错位块报 Tampered", got,
        isinstance(got, str) and "ChunkTamperedError" in got)

# E3d 截断到头部 + 部分索引（索引区不完整）
r, got = try_open(A[:H + 40], "e3d_idx_cut.enc")
r and r.close()
record("E3d", "打开报 IntegrityError(index truncated)", got,
       isinstance(got, str) and "IntegrityError" in got)

# E3e 截断到不足一个头部
r, got = try_open(A[:40], "e3e_hdr_cut.enc")
r and r.close()
record("E3e", "打开报 ChunkCryptError(too small)", got,
       isinstance(got, str) and "ChunkCryptError" in got)

# E3f 尾块文件砍掉尾块后再伪造长度字段？无密钥无法重算 header_mac：
#     只改 total_size 1 字节
m = bytearray(T)
m[8 + 8] ^= 0x01   # total_size 字段内 1 字节（magic8 + chunk_size4 之后）
r, got = try_open(bytes(m), "e3f_meta_flip.enc")
r and r.close()
record("E3f", "打开报 IntegrityError(header)", got,
       isinstance(got, str) and "IntegrityError" in got)

# E3g 文件尾部追加垃圾字节
r, got = try_open(A + b"\x00" * 16, "e3g_append.enc")
r and r.close()
record("E3g", "打开报 IntegrityError(trailing)", got,
       isinstance(got, str) and "IntegrityError" in got)

# E3h 错口令（口令派生的 mac_key 不同，header_mac 失配）
path = save("pwd_copy.enc", PW)
try:
    cc.ChunkReader(path, password="definitely-wrong-password")
    got = "错口令被接受!"
    det = False
except BaseException as exc:           # noqa: BLE001
    got = exc_name(exc)
    det = isinstance(exc, cc.IntegrityError)
record("E3h", "打开报 IntegrityError(header)", got, det)

# -------------------------------------------------------------- 汇总
print("=" * 78)
n_silent = sum(1 for *_x, v in results if v.strip() == "SILENT-ACCEPT")
for eid, expect, got, verdict in results:
    print(f"{eid:5s} {verdict}  {expect}")
print("-" * 78)
print(f"共 {len(results)} 项；被检出 {len(results) - n_silent}；"
      f"静默接受 {n_silent}（E2c 跨文件载荷重放、E2e 整文件版本回滚）")
sys.exit(1 if n_silent != 2 else 0)
