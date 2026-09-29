#!/usr/bin/env python3
"""断点续传归档工具（仅 Python 3 标准库）。

特性：
- 每个条目写入归档后，立即把「已完成条目 + sha256 + 归档偏移」原子落盘到状态文件，
  进程被杀后可在下一条目处续跑，已完成条目跳过。
- 续跑前重新扫描源目录并与快照对比：新增 / 删除 / 修改都会被识别并拒绝继续，
  不会把两次不同状态的源混进同一个归档。
- 归档末尾内嵌清单（条目数、总大小、逐条目 sha256），可独立校验整体完整性。

归档格式：未压缩 tar（追加/截断友好）。状态文件：<archive>.state.json（可指定）。
"""

import argparse
import hashlib
import io
import json
import os
import stat as statmod
import sys
import tarfile

MANIFEST_NAME = ".archive-manifest.json"
STATE_VERSION = 1
_CHUNK = 1024 * 1024


class ArchiveError(Exception):
    """归档过程中的通用错误。"""


class SourceChangedError(ArchiveError):
    """源目录在两次运行之间发生了变化。"""

    def __init__(self, added, removed, modified):
        self.added = list(added)
        self.removed = list(removed)
        self.modified = list(modified)
        parts = []
        if self.added:
            parts.append("新增 %d 项（如 %s）" % (len(self.added), self.added[0]))
        if self.removed:
            parts.append("删除 %d 项（如 %s）" % (len(self.removed), self.removed[0]))
        if self.modified:
            parts.append("修改 %d 项（如 %s）" % (len(self.modified), self.modified[0]))
        super().__init__(
            "源目录已发生变化（%s）；为避免把两次状态混入同一归档，已中止。"
            "请用 --fresh 重新开始。" % "、".join(parts)
        )


def _sha256_path(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


class _HashingReader:
    """包装文件对象，边读边算 sha256；copy_hook 用于测试注入中途故障。"""

    def __init__(self, raw, copy_hook=None):
        self._raw = raw
        self._copy_hook = copy_hook
        self.sha = hashlib.sha256()
        self.copied = 0

    def read(self, size=-1):
        data = self._raw.read(size)
        if data:
            self.sha.update(data)
            self.copied += len(data)
            if self._copy_hook is not None:
                self._copy_hook(self.copied)
        return data

    def close(self):
        self._raw.close()


def _scan(root):
    """扫描源目录，返回 {files: {rel: {size, mtime_ns}}, dirs: [...], links: {rel: target}}。"""
    files, dirs, links = {}, {}, {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        for name in dirnames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            if os.path.islink(full):
                links[rel] = os.readlink(full)
            else:
                dirs[rel] = True
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            st = os.lstat(full)
            if statmod.S_ISREG(st.st_mode):
                files[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
            elif statmod.S_ISLNK(st.st_mode):
                links[rel] = os.readlink(full)
            else:
                raise ArchiveError("不支持的文件类型（非常规文件/目录/符号链接）: %s" % rel)
    return {"files": files, "dirs": sorted(dirs), "links": links}


def _save_state(state_path, state):
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, state_path)


def _load_state(state_path):
    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)
    if state.get("version") != STATE_VERSION:
        raise ArchiveError("状态文件版本不兼容: %s" % state_path)
    return state


def _check_source_unchanged(state, snap, src_root, full_check):
    """对比快照与当前扫描结果；仅元数据变化（touch）且内容校验一致时放行。"""
    old = state["snapshot"]
    completed = state["completed"]

    old_keys = set(old["files"]) | set(old["dirs"]) | set(old["links"])
    new_keys = set(snap["files"]) | set(snap["dirs"]) | set(snap["links"])
    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    modified = []

    for rel in sorted(set(old["files"]) & set(snap["files"])):
        if old["files"][rel] == snap["files"][rel]:
            continue
        meta = completed.get(rel)
        if meta and meta.get("type") == "file":
            # stat 变了：重算摘要，内容没变则仅更新快照（如 touch）
            if _sha256_path(os.path.join(src_root, rel)) == meta["sha256"]:
                old["files"][rel] = snap["files"][rel]
                continue
        modified.append(rel)

    for rel in sorted(set(old["links"]) & set(snap["links"])):
        if old["links"][rel] != snap["links"][rel]:
            modified.append(rel)

    if full_check:
        # 全量复核：已完成条目逐条重算摘要，确认仍然有效
        for rel, meta in sorted(completed.items()):
            if meta.get("type") != "file" or rel in modified:
                continue
            if _sha256_path(os.path.join(src_root, rel)) != meta["sha256"]:
                modified.append(rel)

    if added or removed or modified:
        raise SourceChangedError(added, removed, modified)


def create_archive(src_dir, archive_path, state_path=None, full_check=False,
                   entry_hook=None, copy_hook=None):
    """创建/续传归档。返回内嵌清单 dict。

    entry_hook(rel)：每处理一个待归档条目前调用（测试/进度用）。
    copy_hook(n)：单条目每复制一段后调用，n 为该条目已复制字节数（测试注入中断用）。
    """
    src_dir = os.path.abspath(src_dir)
    archive_path = os.path.abspath(archive_path)
    state_path = state_path or archive_path + ".state.json"

    if not os.path.isdir(src_dir):
        raise ArchiveError("源目录不存在: %s" % src_dir)

    snap = _scan(src_dir)
    if MANIFEST_NAME in snap["files"]:
        raise ArchiveError("源目录包含保留文件名 %s" % MANIFEST_NAME)

    if os.path.exists(state_path):
        state = _load_state(state_path)
        if state["source_root"] != src_dir or state["archive"] != archive_path:
            raise ArchiveError("状态文件与源目录/归档路径不匹配: %s" % state_path)
        if state.get("done"):
            return state["manifest"]
        _check_source_unchanged(state, snap, src_dir, full_check)
    else:
        state = {
            "version": STATE_VERSION,
            "source_root": src_dir,
            "archive": archive_path,
            "snapshot": snap,
            "completed": {},
            "data_end": 0,
            "done": False,
        }
        with open(archive_path, "wb"):
            pass  # 全新归档，清空旧文件
        _save_state(state_path, state)

    # 截断到最后一个完整条目之后，丢弃可能存在的半个条目 / 旧结束标记
    with open(archive_path, "r+b") as f:
        f.truncate(state["data_end"])

    completed = state["completed"]

    def commit(fobj, rel, meta):
        fobj.flush()
        os.fsync(fobj.fileno())
        state["data_end"] = fobj.tell()
        if rel is not None:
            completed[rel] = meta
        _save_state(state_path, state)

    with open(archive_path, "r+b") as fobj:
        fobj.seek(state["data_end"])
        tar = tarfile.open(fileobj=fobj, mode="w", format=tarfile.PAX_FORMAT)
        try:
            for rel in state["snapshot"]["dirs"]:
                if rel in completed:
                    continue
                if entry_hook:
                    entry_hook(rel)
                tar.addfile(tar.gettarinfo(os.path.join(src_dir, rel), arcname=rel))
                commit(fobj, rel, {"type": "dir"})

            for rel, target in sorted(state["snapshot"]["links"].items()):
                if rel in completed:
                    continue
                if entry_hook:
                    entry_hook(rel)
                tar.addfile(tar.gettarinfo(os.path.join(src_dir, rel), arcname=rel))
                commit(fobj, rel, {"type": "link", "target": target})

            for rel in sorted(state["snapshot"]["files"]):
                if rel in completed:
                    continue
                if entry_hook:
                    entry_hook(rel)
                full = os.path.join(src_dir, rel)
                ti = tar.gettarinfo(full, arcname=rel)
                reader = _HashingReader(open(full, "rb"), copy_hook)
                try:
                    tar.addfile(ti, reader)
                finally:
                    reader.close()
                commit(fobj, rel, {
                    "type": "file",
                    "size": ti.size,
                    "sha256": reader.sha.hexdigest(),
                })

            entries = dict(completed)
            manifest = {
                "source_root": src_dir,
                "entries": entries,
                "totals": {
                    "entries": len(entries),
                    "files": sum(1 for m in entries.values() if m["type"] == "file"),
                    "total_size": sum(m.get("size", 0) for m in entries.values()),
                },
            }
            data = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ti = tarfile.TarInfo(MANIFEST_NAME)
            ti.size = len(data)
            ti.mtime = 0
            tar.addfile(ti, io.BytesIO(data))
        finally:
            tar.close()

    state["done"] = True
    state["manifest"] = manifest
    _save_state(state_path, state)
    return manifest


def verify_archive(archive_path):
    """校验归档整体完整性：条目数、总大小、逐条目摘要。返回报告 dict。"""
    archive_path = os.path.abspath(archive_path)
    members = {}
    manifest = None
    try:
        tar = tarfile.open(archive_path, "r")
    except tarfile.TarError as e:
        raise ArchiveError("归档无法读取（可能已损坏）: %s" % e)
    with tar:
        for m in tar:
            if m.name == MANIFEST_NAME:
                manifest = json.loads(tar.extractfile(m).read().decode("utf-8"))
            elif m.isfile():
                h = hashlib.sha256()
                f = tar.extractfile(m)
                for chunk in iter(lambda: f.read(_CHUNK), b""):
                    h.update(chunk)
                members[m.name] = {"type": "file", "size": m.size, "sha256": h.hexdigest()}
            elif m.isdir():
                members[m.name] = {"type": "dir"}
            elif m.islnk() or m.issym():
                members[m.name] = {"type": "link", "target": m.linkname}

    if manifest is None:
        raise ArchiveError("归档缺少内嵌清单 %s，可能是不完整归档" % MANIFEST_NAME)

    errors = []
    entries = manifest["entries"]
    if set(members) != set(entries):
        missing = sorted(set(entries) - set(members))
        extra = sorted(set(members) - set(entries))
        if missing:
            errors.append("归档缺少条目: %s" % missing[:5])
        if extra:
            errors.append("归档多出条目: %s" % extra[:5])
    for rel in sorted(set(members) & set(entries)):
        if members[rel] != entries[rel]:
            errors.append("条目校验不一致: %s" % rel)

    totals = manifest["totals"]
    if totals["entries"] != len(entries):
        errors.append("清单条目数自相矛盾")
    if totals["total_size"] != sum(m.get("size", 0) for m in entries.values()):
        errors.append("清单总大小自相矛盾")

    if errors:
        raise ArchiveError("归档完整性校验失败: " + "; ".join(errors))
    return {
        "archive": archive_path,
        "entries": totals["entries"],
        "files": totals["files"],
        "total_size": totals["total_size"],
        "ok": True,
    }


def _cmd_create(args):
    if args.fresh:
        for p in (args.archive, args.state or args.archive + ".state.json"):
            if os.path.exists(p):
                os.remove(p)
    try:
        manifest = create_archive(
            args.source, args.archive,
            state_path=args.state, full_check=args.full_check,
        )
    except SourceChangedError as e:
        print("错误: %s" % e, file=sys.stderr)
        return 2
    except ArchiveError as e:
        print("错误: %s" % e, file=sys.stderr)
        return 1
    t = manifest["totals"]
    print("归档完成: %d 个条目（%d 个文件，共 %d 字节） -> %s"
          % (t["entries"], t["files"], t["total_size"], args.archive))
    return 0


def _cmd_verify(args):
    try:
        report = verify_archive(args.archive)
    except ArchiveError as e:
        print("校验失败: %s" % e, file=sys.stderr)
        return 1
    print("校验通过: %(entries)d 个条目（%(files)d 个文件，共 %(total_size)d 字节）" % report)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="断点续传归档工具（标准库实现）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create", help="创建/续传归档")
    p.add_argument("source", help="源目录")
    p.add_argument("archive", help="归档文件路径")
    p.add_argument("--state", help="状态文件路径（默认 <archive>.state.json）")
    p.add_argument("--fresh", action="store_true", help="丢弃旧状态，从头开始")
    p.add_argument("--full-check", action="store_true",
                   help="续跑时对已完成条目逐条重算 sha256（默认仅靠 size+mtime 快检）")
    p.set_defaults(func=_cmd_create)

    p = sub.add_parser("verify", help="校验归档完整性")
    p.add_argument("archive", help="归档文件路径")
    p.set_defaults(func=_cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
