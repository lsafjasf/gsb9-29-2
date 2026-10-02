#!/usr/bin/env python3
"""resumable_archive - 支持断点续传的目录归档工具（仅依赖 Python 3 标准库）。

归档格式（追加式，便于断点续传）::

    MAGIC                          b"RARC0001\\n"
    条目 * N                       u64 头长度 + JSON 头 + 原始数据(仅文件)
    结束记录                       u64 头长度 + JSON {"type": "end", ...}

JSON 头字段: path / type("f"|"d") / size / mtime_ns
结束记录字段: entries(总条目数) / files(文件数) / total_data_bytes(数据总大小)
             / sha256{path: 摘要}(逐条目摘要) / manifest_sha256(摘要清单的摘要)

状态文件（<归档>.state.json）记录：源目录快照、已完成条目及其 sha256、
安全截断偏移 archive_offset。续跑时：
  1. 重新扫描源目录并与快照比对 —— 新增/删除/修改一律拒绝续跑（不混状态）；
  2. 将归档截断到 archive_offset，丢弃中断时未写完的半个条目；
  3. 跳过已完成条目（默认按 size+mtime_ns 校验仍有效，--verify-hashes 重新算 sha256）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat as statmod
import struct
import sys
import time

MAGIC = b"RARC0001\n"
_LEN = struct.Struct(">Q")
_CHUNK = 4 * 1024 * 1024
STATE_VERSION = 1
MAX_HEADER = 1 << 31


class ArchiveError(Exception):
    """归档/校验过程中的通用错误。"""


class SourceChangedError(ArchiveError):
    """续跑时发现源目录与首次运行的快照不一致。"""

    def __init__(self, added, removed, modified):
        self.added = list(added)
        self.removed = list(removed)
        self.modified = list(modified)
        parts = []
        if added:
            parts.append("新增 %d 项" % len(added))
        if removed:
            parts.append("删除 %d 项" % len(removed))
        if modified:
            parts.append("修改 %d 项" % len(modified))
        super().__init__(
            "源目录在两次运行之间发生了变化（%s）；"
            "为保证归档一致性已拒绝续跑，请用 --fresh 重新开始。" % "、".join(parts)
        )


# ---------------------------------------------------------------- 源目录快照

def scan_source(root):
    """扫描源目录，返回 {相对路径: {"t","s","m"}} 快照（确定性顺序）。"""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise ArchiveError("源目录不存在或不是目录: %s" % root)
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        filenames.sort()
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir != ".":
            st = os.lstat(dirpath)
            snap[rel_dir] = {"t": "d", "s": 0, "m": st.st_mtime_ns}
        for name in filenames:
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            if not statmod.S_ISREG(st.st_mode):
                raise ArchiveError("不支持的文件类型（仅支持普通文件与目录）: %s" % path)
            rel = name if rel_dir == "." else os.path.join(rel_dir, name)
            snap[os.path.normpath(rel)] = {"t": "f", "s": st.st_size, "m": st.st_mtime_ns}
    return snap


def diff_snapshots(old, new):
    """返回 (added, removed, modified) 三个已排序列表。"""
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    modified = sorted(k for k in set(old) & set(new) if old[k] != new[k])
    return added, removed, modified


# ---------------------------------------------------------------- 状态文件

def default_state_path(archive_path):
    return archive_path + ".state.json"


def _save_state(state_path, state):
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, state_path)


def _load_state(state_path):
    with open(state_path, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    if state.get("version") != STATE_VERSION:
        raise ArchiveError("状态文件版本不兼容: %s" % state_path)
    return state


def _verify_completed_hashes(src, state):
    """严格模式：对已完成条目重新计算 sha256，确认其内容仍然有效。"""
    for rel in sorted(state["completed"]):
        info = state["completed"][rel]
        if info["sha256"] is None:
            continue
        path = os.path.join(src, rel)
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(_CHUNK), b""):
                digest.update(chunk)
        if digest.hexdigest() != info["sha256"]:
            raise ArchiveError("已完成条目校验失败（内容已变化）: %s" % rel)


# ---------------------------------------------------------------- 归档创建

def create_archive(src, dst, state_path=None, fresh=False, verify_hashes=False,
                   abort_hook=None, progress=None):
    """创建（或续传）归档。

    参数:
        src           源目录
        dst           归档文件路径
        state_path    状态文件路径（默认 <dst>.state.json）
        fresh         忽略已有状态，从头开始
        verify_hashes 续跑时对已完成条目重新计算 sha256（默认只做 stat 校验）
        abort_hook    每写完一个数据块回调 abort_hook(本次字节数)，测试用
        progress      进度回调 progress(已完成条目数, 总条目数)
    返回:
        统计信息 dict
    """
    src = os.path.abspath(src)
    state_path = state_path or default_state_path(dst)
    started = time.monotonic()

    if fresh and os.path.exists(state_path):
        os.remove(state_path)

    resumed = False
    if os.path.exists(state_path):
        state = _load_state(state_path)
        if state.get("done"):
            raise ArchiveError("该归档已完成；如需重做请使用 --fresh")
        if state["source_root"] != src:
            raise ArchiveError("状态文件属于另一个源目录: %s" % state["source_root"])
        current = scan_source(src)
        added, removed, modified = diff_snapshots(state["snapshot"], current)
        if added or removed or modified:
            raise SourceChangedError(added, removed, modified)
        if verify_hashes:
            _verify_completed_hashes(src, state)
        resumed = True
    else:
        state = {
            "version": STATE_VERSION,
            "source_root": src,
            "archive": os.path.abspath(dst),
            "snapshot": scan_source(src),
            "completed": {},
            "archive_offset": 0,
            "done": False,
        }

    snapshot = state["snapshot"]
    completed = state["completed"]
    total_entries = len(snapshot)

    if resumed:
        if not os.path.exists(dst):
            raise ArchiveError("状态文件存在但归档文件缺失: %s" % dst)
        out = open(dst, "r+b")
        out.truncate(state["archive_offset"])  # 丢弃中断时未写完的半个条目
        out.seek(state["archive_offset"])
    else:
        out = open(dst, "wb")
        out.write(MAGIC)
        state["archive_offset"] = out.tell()
        _save_state(state_path, state)

    try:
        for rel in sorted(snapshot):
            if rel in completed:
                continue
            meta = snapshot[rel]
            header = {
                "path": rel,
                "type": meta["t"],
                "size": meta["s"],
                "mtime_ns": meta["m"],
            }
            header_bytes = json.dumps(header, ensure_ascii=False).encode("utf-8")
            out.write(_LEN.pack(len(header_bytes)))
            out.write(header_bytes)
            if meta["t"] == "f":
                digest = hashlib.sha256()
                remaining = meta["s"]
                with open(os.path.join(src, rel), "rb") as inf:
                    while remaining > 0:
                        chunk = inf.read(min(_CHUNK, remaining))
                        if not chunk:
                            raise ArchiveError("文件在归档过程中被截短: %s" % rel)
                        remaining -= len(chunk)
                        digest.update(chunk)
                        out.write(chunk)
                        if abort_hook is not None:
                            abort_hook(len(chunk))
                completed[rel] = {"sha256": digest.hexdigest(), "size": meta["s"]}
            else:
                completed[rel] = {"sha256": None, "size": 0}
                if abort_hook is not None:
                    abort_hook(0)
            out.flush()
            state["archive_offset"] = out.tell()
            _save_state(state_path, state)
            if progress is not None:
                progress(len(completed), total_entries)

        file_hashes = {r: i["sha256"] for r, i in completed.items() if i["sha256"]}
        manifest_bytes = json.dumps(file_hashes, sort_keys=True).encode("utf-8")
        end_record = {
            "type": "end",
            "entries": len(completed),
            "files": len(file_hashes),
            "total_data_bytes": sum(i["size"] for i in completed.values()),
            "sha256": file_hashes,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        }
        end_bytes = json.dumps(end_record, ensure_ascii=False).encode("utf-8")
        out.write(_LEN.pack(len(end_bytes)))
        out.write(end_bytes)
        out.flush()
        os.fsync(out.fileno())
        state["archive_offset"] = out.tell()
        state["done"] = True
        _save_state(state_path, state)
    finally:
        out.close()

    return {
        "entries": len(completed),
        "files": len(file_hashes),
        "total_data_bytes": end_record["total_data_bytes"],
        "resumed": resumed,
        "elapsed": time.monotonic() - started,
    }


# ---------------------------------------------------------------- 归档校验

def _read_exact(fh, n, what):
    data = fh.read(n)
    if len(data) != n:
        raise ArchiveError("归档被截断：读取%s时只得到 %d/%d 字节" % (what, len(data), n))
    return data


def verify_archive(path, state_path=None):
    """校验归档整体完整性：条目数、总大小、逐条目 sha256。

    可选地同时与状态文件中的完成清单交叉核对。
    返回统计信息 dict；不一致时抛出 ArchiveError。
    """
    entries = 0
    files = 0
    total_data = 0
    hashes = {}
    with open(path, "rb") as fh:
        if fh.read(len(MAGIC)) != MAGIC:
            raise ArchiveError("不是本格式的归档文件（MAGIC 不匹配）: %s" % path)
        while True:
            len_buf = fh.read(_LEN.size)
            if not len_buf:
                raise ArchiveError("归档被截断：缺少结束记录")
            if len(len_buf) != _LEN.size:
                raise ArchiveError("归档被截断：条目标记不完整")
            (hlen,) = _LEN.unpack(len_buf)
            if hlen > MAX_HEADER:
                raise ArchiveError("条目标记长度异常: %d" % hlen)
            try:
                header = json.loads(_read_exact(fh, hlen, "条目头").decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise ArchiveError("条目头损坏（归档数据已损坏）")
            if header.get("type") == "end":
                end = header
                break
            entries += 1
            etype = header.get("type")
            if etype == "f":
                files += 1
                size = header["size"]
                total_data += size
                digest = hashlib.sha256()
                remaining = size
                while remaining > 0:
                    chunk = fh.read(min(_CHUNK, remaining))
                    if not chunk:
                        raise ArchiveError("归档被截断：条目数据不完整: %s" % header["path"])
                    remaining -= len(chunk)
                    digest.update(chunk)
                hashes[header["path"]] = digest.hexdigest()
            elif etype == "d":
                pass
            else:
                raise ArchiveError("未知条目类型: %r" % etype)
        if fh.read(1):
            raise ArchiveError("结束记录之后存在多余数据")

    if end.get("entries") != entries:
        raise ArchiveError("条目数不一致: 结束记录=%s 实际=%s" % (end.get("entries"), entries))
    if end.get("total_data_bytes") != total_data:
        raise ArchiveError("总大小不一致: 结束记录=%s 实际=%s" % (end.get("total_data_bytes"), total_data))
    if end.get("sha256") != hashes:
        raise ArchiveError("逐条目摘要与结束记录不一致")
    manifest_bytes = json.dumps(hashes, sort_keys=True).encode("utf-8")
    if end.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest():
        raise ArchiveError("摘要清单的整体摘要不一致")

    if state_path and os.path.exists(state_path):
        state = _load_state(state_path)
        expected = {r: i["sha256"] for r, i in state["completed"].items() if i["sha256"]}
        if expected != hashes:
            raise ArchiveError("归档内容与状态文件记录的完成清单不一致")
        if len(state["completed"]) != entries:
            raise ArchiveError("条目数与状态文件不一致")

    return {"entries": entries, "files": files, "total_data_bytes": total_data}


# ---------------------------------------------------------------- CLI

def _fmt_size(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


def main(argv=None):
    parser = argparse.ArgumentParser(description="支持断点续传的目录归档工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("create", help="创建/续传归档")
    pc.add_argument("src", help="源目录")
    pc.add_argument("dst", help="归档文件")
    pc.add_argument("--state", help="状态文件路径（默认 <归档>.state.json）")
    pc.add_argument("--fresh", action="store_true", help="忽略已有状态，从头开始")
    pc.add_argument("--verify-hashes", action="store_true",
                    help="续跑时对已完成条目重新计算 sha256（更慢但更严格）")

    pv = sub.add_parser("verify", help="校验归档完整性")
    pv.add_argument("archive", help="归档文件")
    pv.add_argument("--state", help="同时与该状态文件交叉核对")

    args = parser.parse_args(argv)

    if args.cmd == "create":
        try:
            stats = create_archive(args.src, args.dst, state_path=args.state,
                                   fresh=args.fresh, verify_hashes=args.verify_hashes)
        except SourceChangedError as exc:
            print("错误: %s" % exc, file=sys.stderr)
            for label, items in (("新增", exc.added), ("删除", exc.removed), ("修改", exc.modified)):
                for item in items[:20]:
                    print("  [%s] %s" % (label, item), file=sys.stderr)
            return 2
        except ArchiveError as exc:
            print("错误: %s" % exc, file=sys.stderr)
            return 1
        print("完成%s: 条目=%d 文件=%d 数据=%s 耗时=%.2fs" % (
            "（续传）" if stats["resumed"] else "",
            stats["entries"], stats["files"],
            _fmt_size(stats["total_data_bytes"]), stats["elapsed"]))
        return 0

    if args.cmd == "verify":
        try:
            stats = verify_archive(args.archive, state_path=args.state)
        except ArchiveError as exc:
            print("校验失败: %s" % exc, file=sys.stderr)
            return 1
        print("校验通过: 条目=%d 文件=%d 数据=%s" % (
            stats["entries"], stats["files"], _fmt_size(stats["total_data_bytes"])))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
