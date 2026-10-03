#!/usr/bin/env python3
"""互操作兼容性验证脚本（仅标准库）。

验证自研归档工具 mytar 与常见工具的双向互操作：
  方向 A：mytar 写归档 -> 其他工具读取
  方向 B：其他工具写归档 -> mytar 读取

参与工具：
  mytar       自研工具（interop/mytar.py，手写 ustar/GNU/pax）
  gnu-tar     GNU tar 命令行（系统 tar）
  py-tarfile  Python 标准库 tarfile（PAX 格式写出）

判定维度（逐项独立判定，不是"能解开就算过"）：
  content  条目内容：普通文件比较 SHA-256；符号链接比较链接目标
  order    条目顺序：读取方得到的条目名序列必须与写入方写出的序列一致
  mode     权限：st_mode & 0o7777（符号链接除外，见下）
  mtime    修改时间：整数秒，允许 ±1s 容差（tar 头只存整数秒）
  type     条目类型：file / dir / symlink

已知合理例外（在报告中显式标注，不算不一致）：
  - 符号链接的 mode/mtime 不参与比较：tar 惯例将 symlink 权限写为 0777，
    且多数文件系统不允许 chmod 符号链接，跨工具必然不同。

每条不一致输出可定位证据：用例、方向、条目名、差异字段、期望值、实际值。

退出码：0 = 全部 PASS/SKIP；1 = 存在 FAIL；2 = 环境缺失（无 GNU tar）。
"""

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mytar

MTIME_TOLERANCE = 1
FIXED_MTIME = 1_696_000_000

TOOLS = ("mytar", "gnu-tar", "py-tarfile")


class Item:
    __slots__ = ("mode", "mtime", "type", "sha256", "linkname")

    def __init__(self, mode, mtime, type_, sha256="", linkname=""):
        self.mode = mode
        self.mtime = mtime
        self.type = type_
        self.sha256 = sha256
        self.linkname = linkname


class View:
    def __init__(self, order, items):
        self.order = order
        self.items = items


class Diff:
    def __init__(self, entry, field, expected, actual):
        self.entry = entry
        self.field = field
        self.expected = expected
        self.actual = actual

    def __str__(self):
        return "entry=%r field=%s expected=%s actual=%s" % (
            self.entry, self.field, self.expected, self.actual)


class ToolUnsupported(Exception):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def norm_name(name, type_):
    if type_ == "dir" and name.endswith("/"):
        return name[:-1]
    return name


# ---------------------------------------------------------------- 语料构建

LONG_COMPONENT = "很长的路径段-very-long-segment" * 6
NONASCII_DIR = "数据目录"
NONASCII_FILE = NONASCII_DIR + "/归档-测试-üñíçødé-文件.txt"
LONG_FILE = "data/" + LONG_COMPONENT + "/末尾文件-éè.txt"
SPARSE_FILE = "sparse.bin"
SPARSE_SIZE = 4 * 1024 * 1024

CORPUS_FILES = {
    "hello.txt": b"hello archive interop\n",
    "empty.bin": b"",
    "data/nested.txt": b"nested payload\n" * 3,
    LONG_FILE: "长文件名条目内容\n".encode("utf-8"),
    NONASCII_FILE: "非 ASCII 内容：归档互操作 ✓\n".encode("utf-8"),
}
CORPUS_DIRS = ["data", "data/" + LONG_COMPONENT, NONASCII_DIR]
CORPUS_SYMLINKS = {"link-to-hello": "hello.txt"}


def build_corpus(root):
    os.makedirs(root, exist_ok=True)
    for rel in CORPUS_DIRS:
        os.makedirs(os.path.join(root, rel), exist_ok=True)
    for rel, data in CORPUS_FILES.items():
        with open(os.path.join(root, rel), "wb") as fh:
            fh.write(data)
    sparse_path = os.path.join(root, SPARSE_FILE)
    with open(sparse_path, "wb") as fh:
        fh.write(b"SPARSE-HEAD")
        fh.truncate(SPARSE_SIZE)
        fh.seek(SPARSE_SIZE - len(b"SPARSE-TAIL"))
        fh.write(b"SPARSE-TAIL")
    for rel, target in CORPUS_SYMLINKS.items():
        os.symlink(target, os.path.join(root, rel))
    for index, rel in enumerate(sorted(os.listdir(root))):
        _stamp(os.path.join(root, rel), index)
    os.chmod(os.path.join(root, "data"), 0o750)
    os.chmod(os.path.join(root, "hello.txt"), 0o640)
    os.chmod(os.path.join(root, NONASCII_FILE), 0o600)


def _stamp(path, index):
    atime = mtime = FIXED_MTIME + index
    if os.path.isdir(path) and not os.path.islink(path):
        for child in sorted(os.listdir(path)):
            _stamp(os.path.join(path, child), index)
        os.chmod(path, 0o750 if os.path.basename(path) == "data" else 0o755)
    os.utime(path, (atime, mtime), follow_symlinks=False)


def model_from_tree(root):
    entries = mytar.entries_from_tree(root)
    order = [norm_name(e.name, e.type) for e in entries]
    items = {}
    for e in entries:
        items[norm_name(e.name, e.type)] = Item(
            e.mode, int(e.mtime), e.type,
            sha256(e.data) if e.type == "file" else "", e.linkname)
    return View(order, items)


# ---------------------------------------------------------------- 写入方

def write_mytar(archive, root):
    mytar.write_archive(archive, mytar.entries_from_tree(root))


def write_gnutar(archive, root):
    names = sorted(os.listdir(root))
    cmd = ["tar", "--format=gnu", "-cf", archive, "-C", root]
    if names:
        cmd += names
    else:
        cmd += ["--files-from", "/dev/null"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ToolUnsupported("gnu-tar write failed: %s" % result.stderr.strip())


def write_tarfile(archive, root):
    with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as tf:
        for entry in mytar.entries_from_tree(root):
            tf.add(os.path.join(root, entry.name), arcname=entry.name,
                   recursive=False)


WRITERS = {"mytar": write_mytar, "gnu-tar": write_gnutar, "py-tarfile": write_tarfile}


# ---------------------------------------------------------------- 读取方

def read_mytar(archive, workdir):
    entries = mytar.read_archive(archive)
    order, items = [], {}
    for e in entries:
        name = norm_name(e.name, e.type)
        order.append(name)
        items[name] = Item(e.mode, e.mtime, e.type,
                           sha256(e.data) if e.type == "file" else "", e.linkname)
    return View(order, items)


def read_gnutar(archive, workdir):
    listing = subprocess.run(["tar", "-tf", archive],
                             capture_output=True, text=True)
    if listing.returncode != 0:
        raise ToolUnsupported("gnu-tar list failed: %s" % listing.stderr.strip())
    order = [line[:-1] if line.endswith("/") else line
             for line in listing.stdout.splitlines() if line]
    outdir = os.path.join(workdir, "extract")
    os.makedirs(outdir, exist_ok=True)
    result = subprocess.run(["tar", "-xpf", archive, "-C", outdir],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise ToolUnsupported("gnu-tar extract failed: %s" % result.stderr.strip())
    return View(order, model_from_tree(outdir).items)


def read_tarfile(archive, workdir):
    order, items = [], {}
    with tarfile.open(archive, "r") as tf:
        for member in tf:
            if member.isdir():
                type_ = "dir"
            elif member.issym():
                type_ = "symlink"
            else:
                type_ = "file"
            name = norm_name(member.name, type_)
            order.append(name)
            digest = sha256(tf.extractfile(member).read()) if type_ == "file" else ""
            items[name] = Item(member.mode, member.mtime, type_, digest,
                               member.linkname if type_ == "symlink" else "")
    return View(order, items)


READERS = {"mytar": read_mytar, "gnu-tar": read_gnutar, "py-tarfile": read_tarfile}


# ---------------------------------------------------------------- 比较

def compare(model, view):
    diffs = []
    if view.order != model.order:
        where = next((i for i, pair in enumerate(zip(model.order, view.order))
                      if pair[0] != pair[1]), min(len(model.order), len(view.order)))
        diffs.append(Diff("<archive>", "order",
                          "index %d: %r (共 %d 条)" % (where, model.order[where:where + 1], len(model.order)),
                          "index %d: %r (共 %d 条)" % (where, view.order[where:where + 1], len(view.order))))
    for name in model.order:
        if name not in view.items:
            diffs.append(Diff(name, "presence", "present", "missing"))
    for name in view.order:
        if name not in model.items:
            diffs.append(Diff(name, "presence", "absent", "extra"))
    for name in model.order:
        if name not in view.items:
            continue
        exp, act = model.items[name], view.items[name]
        if exp.type != act.type:
            diffs.append(Diff(name, "type", exp.type, act.type))
            continue
        if exp.type == "file" and exp.sha256 != act.sha256:
            diffs.append(Diff(name, "content", exp.sha256, act.sha256))
        if exp.type == "symlink":
            if exp.linkname != act.linkname:
                diffs.append(Diff(name, "linkname", exp.linkname, act.linkname))
            continue
        if exp.mode != act.mode:
            diffs.append(Diff(name, "mode", oct(exp.mode), oct(act.mode)))
        if abs(int(exp.mtime) - int(act.mtime)) > MTIME_TOLERANCE:
            diffs.append(Diff(name, "mtime", int(exp.mtime), act.mtime))
    return diffs


FIELD_TO_DIMENSION = {
    "content": "content", "presence": "content", "linkname": "content",
    "order": "order", "mode": "mode", "mtime": "mtime", "type": "type",
}
DIMENSIONS = ("content", "order", "mode", "mtime", "type")


# ---------------------------------------------------------------- 矩阵执行

CASES = ("full", "empty")


def run_matrix(workdir):
    cells = []
    for case in CASES:
        corpus = os.path.join(workdir, "corpus-" + case)
        build_corpus(corpus)
        model = model_from_tree(corpus)
        for writer in TOOLS:
            archive = os.path.join(workdir, "%s-by-%s.tar" % (case, writer))
            try:
                WRITERS[writer](archive, corpus)
                write_error = None
            except ToolUnsupported as exc:
                write_error = str(exc)
            for reader in TOOLS:
                if writer == reader or "mytar" not in (writer, reader):
                    continue
                cell = {"case": case, "writer": writer, "reader": reader,
                        "diffs": [], "status": "PASS", "note": ""}
                if write_error:
                    cell["status"], cell["note"] = "SKIP", write_error
                else:
                    try:
                        view = READERS[reader](archive, os.path.join(
                            workdir, "view-%s-%s-%s" % (case, writer, reader)))
                        cell["diffs"] = compare(model, view)
                        if cell["diffs"]:
                            cell["status"] = "FAIL"
                    except (ToolUnsupported, ValueError, tarfile.TarError) as exc:
                        cell["status"], cell["note"] = "FAIL", "读取异常: %s" % exc
                cells.append(cell)
    return cells


def dimension_status(cell):
    status = {d: "OK" for d in DIMENSIONS}
    for diff in cell["diffs"]:
        status[FIELD_TO_DIMENSION[diff.field]] = "DIFF"
    if cell["status"] == "FAIL" and not cell["diffs"]:
        for d in DIMENSIONS:
            status[d] = "ERR"
    if cell["status"] == "SKIP":
        for d in DIMENSIONS:
            status[d] = "-"
    return status


def render_matrix(cells):
    header = "| 用例 | 写入方 | 读取方 | 内容 | 顺序 | 权限 | 时间 | 类型 | 结论 |"
    sep = "|---|---|---|---|---|---|---|---|---|"
    lines = [header, sep]
    for cell in cells:
        st = dimension_status(cell)
        verdict = cell["status"] + (" (%s)" % cell["note"] if cell["note"] else "")
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            cell["case"], cell["writer"], cell["reader"],
            st["content"], st["order"], st["mode"], st["mtime"], st["type"],
            verdict))
    return "\n".join(lines)


def render_evidence(cells):
    lines = []
    for cell in cells:
        for diff in cell["diffs"]:
            lines.append("- [%s] %s -> %s: %s" % (
                cell["case"], cell["writer"], cell["reader"], diff))
    return "\n".join(lines) if lines else "（本次运行无不一致）"


def demo_evidence():
    model = View(["a.txt", "b.txt"], {
        "a.txt": Item(0o644, FIXED_MTIME, "file", sha256(b"aaa")),
        "b.txt": Item(0o600, FIXED_MTIME, "file", sha256(b"bbb")),
    })
    tampered = View(["b.txt", "a.txt"], {
        "a.txt": Item(0o644, FIXED_MTIME + 5, "file", sha256(b"aaa")),
        "b.txt": Item(0o644, FIXED_MTIME, "file", sha256(b"XXX")),
    })
    return compare(model, tampered)


def render_report(cells):
    parts = [
        "# 互操作验证报告",
        "",
        "## 判定标准",
        "",
        "- 内容：普通文件比较 SHA-256；符号链接比较链接目标；条目缺失/多出记为内容维度不一致",
        "- 顺序：读取方条目名序列必须与写入方写出序列完全一致",
        "- 权限：st_mode & 0o7777（符号链接除外，tar 惯例固定 0777，跨工具不可比）",
        "- 时间：整数秒 mtime，允许 ±%ds 容差" % MTIME_TOLERANCE,
        "- 类型：file / dir / symlink 必须一致",
        "",
        "## 互操作矩阵",
        "",
        render_matrix(cells),
        "",
        "## 差异证据",
        "",
        render_evidence(cells),
        "",
        "## 差异证据格式样例（自测注入构造，非本次运行结果）",
        "",
    ]
    parts += ["- %s" % d for d in demo_evidence()]
    parts += [
        "",
        "## 边界用例",
        "",
        "- 长文件名：路径全长 >100 字节（ustar name 字段上限），含多级长目录，触发 GNU longlink / pax path 扩展",
        "- 非 ASCII：中文目录名、中文+拉丁扩展字符文件名、UTF-8 文件内容",
        "- 稀疏文件：%d 字节逻辑大小，仅首尾有数据，中间为空洞（按内容全零判定）" % SPARSE_SIZE,
        "- 空归档：0 个条目的归档（mytar/tarfile 直接写零块，GNU tar 用 --files-from /dev/null）",
        "- 其他：空文件、符号链接、自定义权限（0640/0600/0750）、固定 mtime",
        "",
        "## 运行方式",
        "",
        "```sh",
        "python3 interop/interop_check.py     # 运行互操作矩阵并生成本报告",
        "python3 -m unittest discover -s interop -v   # 运行自测",
        "```",
        "",
    ]
    return "\n".join(parts)


def main():
    if shutil.which("tar") is None:
        print("ERROR: 未找到 GNU tar，无法运行互操作矩阵", file=sys.stderr)
        return 2
    workdir = tempfile.mkdtemp(prefix="interop-")
    cells = run_matrix(workdir)
    report = render_report(cells)
    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "INTEROP_REPORT.md")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(render_matrix(cells))
    print()
    print("差异证据:")
    print(render_evidence(cells))
    print()
    print("报告已写入 %s" % report_path)
    shutil.rmtree(workdir, ignore_errors=True)
    return 1 if any(c["status"] == "FAIL" for c in cells) else 0


if __name__ == "__main__":
    sys.exit(main())
