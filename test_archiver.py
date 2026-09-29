#!/usr/bin/env python3
"""archiver 的完整性与断点续传测试（标准库 unittest）。"""

import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import archiver
from archiver import (
    ArchiveError,
    SourceChangedError,
    create_archive,
    verify_archive,
)


class InjectedFailure(Exception):
    pass


def make_file(path, size, seed=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    block = (seed * 4096)[:4096]
    with open(path, "wb") as f:
        for _ in range(size // len(block)):
            f.write(block)
        f.write(block[: size % len(block)])


def tree_files(root):
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            out[os.path.relpath(full, root)] = archiver._sha256_path(full)
    return out


def extract_listing(archive_path):
    """从归档读出 {rel: sha256}（仅文件）。"""
    out = {}
    with tarfile.open(archive_path, "r") as tar:
        for m in tar:
            if m.isfile() and m.name != archiver.MANIFEST_NAME:
                out[m.name] = archiver.hashlib.sha256(tar.extractfile(m).read()).hexdigest()
    return out


class ArchiverTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="archiver-test-")
        self.src = os.path.join(self.tmp, "src")
        os.makedirs(self.src)
        self.archive = os.path.join(self.tmp, "out.tar")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestBasic(ArchiverTestBase):
    def test_empty_directory(self):
        manifest = create_archive(self.src, self.archive)
        self.assertEqual(manifest["totals"]["entries"], 0)
        self.assertEqual(manifest["totals"]["total_size"], 0)
        report = verify_archive(self.archive)
        self.assertTrue(report["ok"])
        self.assertEqual(report["entries"], 0)

    def test_single_file(self):
        make_file(os.path.join(self.src, "hello.bin"), 100_000, b"a")
        manifest = create_archive(self.src, self.archive)
        self.assertEqual(manifest["totals"]["files"], 1)
        self.assertEqual(manifest["totals"]["total_size"], 100_000)
        self.assertEqual(
            manifest["entries"]["hello.bin"]["sha256"],
            archiver._sha256_path(os.path.join(self.src, "hello.bin")),
        )
        self.assertTrue(verify_archive(self.archive)["ok"])

    def test_nested_tree_with_empty_dirs_and_symlink(self):
        make_file(os.path.join(self.src, "a/b/c.bin"), 50_000, b"c")
        make_file(os.path.join(self.src, "a/d.bin"), 1, b"d")
        os.makedirs(os.path.join(self.src, "empty/sub"))
        os.symlink("a/d.bin", os.path.join(self.src, "link"))
        manifest = create_archive(self.src, self.archive)
        self.assertTrue(verify_archive(self.archive)["ok"])
        kinds = {r: m["type"] for r, m in manifest["entries"].items()}
        self.assertEqual(kinds["empty"], "dir")
        self.assertEqual(kinds["empty/sub"], "dir")
        self.assertEqual(kinds["link"], "link")
        self.assertEqual(manifest["entries"]["link"]["target"], "a/d.bin")
        # 归档内文件摘要与源一致
        src_hashes = tree_files(self.src)
        arc_hashes = extract_listing(self.archive)
        self.assertEqual(src_hashes, arc_hashes)


class TestResume(ArchiverTestBase):
    def _populate(self, nfiles=8, size=200_000):
        for i in range(nfiles):
            make_file(os.path.join(self.src, "dir%d" % (i % 3), "f%02d.bin" % i),
                      size, bytes([i]) * 3)

    def test_interrupt_mid_entry_then_resume(self):
        self._populate()
        seen = []

        def copy_hook(n):
            if n >= 100_000:  # 条目复制到一半时“宕机”
                raise InjectedFailure()

        def entry_hook(rel):
            seen.append(rel)
            if len(seen) >= 4:  # 处理到第 4 个条目时注入故障
                raise InjectedFailure()

        with self.assertRaises(InjectedFailure):
            create_archive(self.src, self.archive, entry_hook=entry_hook,
                           copy_hook=copy_hook)

        state = json.load(open(self.archive + ".state.json"))
        done_after_crash = set(state["completed"])
        self.assertTrue(done_after_crash)  # 已有部分条目完成
        self.assertLess(len(done_after_crash), len(seen) + len(state["snapshot"]["dirs"]))

        # 续跑：统计实际被重新处理的条目
        retried = []
        manifest = create_archive(self.src, self.archive,
                                  entry_hook=retried.append)
        # 已完成条目被跳过，没有重复处理
        self.assertFalse(done_after_crash & set(retried))
        self.assertTrue(verify_archive(self.archive)["ok"])
        self.assertEqual(tree_files(self.src), extract_listing(self.archive))
        self.assertEqual(manifest["totals"]["files"], 8)

    def test_resume_is_idempotent_when_done(self):
        self._populate(nfiles=3, size=10_000)
        create_archive(self.src, self.archive)
        with open(self.archive, "rb") as f:
            before = f.read()
        create_archive(self.src, self.archive)  # 已完成，直接返回
        with open(self.archive, "rb") as f:
            self.assertEqual(before, f.read())

    def test_interrupted_entry_not_partially_kept(self):
        # 单个大文件，复制到一半中断；续传后归档里必须是完整内容
        make_file(os.path.join(self.src, "big.bin"), 3_000_000, b"z")

        def copy_hook(n):
            if n >= 1_500_000:
                raise InjectedFailure()

        with self.assertRaises(InjectedFailure):
            create_archive(self.src, self.archive, copy_hook=copy_hook)
        create_archive(self.src, self.archive)
        self.assertEqual(tree_files(self.src), extract_listing(self.archive))
        self.assertTrue(verify_archive(self.archive)["ok"])


class TestSourceChangeDetection(ArchiverTestBase):
    def _crash_after_first_file(self):
        make_file(os.path.join(self.src, "a.bin"), 10_000, b"a")
        make_file(os.path.join(self.src, "b.bin"), 10_000, b"b")
        calls = []

        def entry_hook(rel):
            calls.append(rel)
            if rel == "b.bin":
                raise InjectedFailure()

        with self.assertRaises(InjectedFailure):
            create_archive(self.src, self.archive, entry_hook=entry_hook)

    def test_added_file_detected(self):
        self._crash_after_first_file()
        make_file(os.path.join(self.src, "new.bin"), 5, b"n")
        with self.assertRaises(SourceChangedError) as ctx:
            create_archive(self.src, self.archive)
        self.assertEqual(ctx.exception.added, ["new.bin"])

    def test_removed_file_detected(self):
        self._crash_after_first_file()
        os.remove(os.path.join(self.src, "b.bin"))
        with self.assertRaises(SourceChangedError) as ctx:
            create_archive(self.src, self.archive)
        self.assertEqual(ctx.exception.removed, ["b.bin"])

    def test_modified_file_detected(self):
        self._crash_after_first_file()
        make_file(os.path.join(self.src, "b.bin"), 10_001, b"B")
        with self.assertRaises(SourceChangedError) as ctx:
            create_archive(self.src, self.archive)
        self.assertEqual(ctx.exception.modified, ["b.bin"])

    def test_modified_completed_file_detected(self):
        # 已完成条目被改动：stat 变化触发重算，摘要不一致 -> 拒绝续跑
        self._crash_after_first_file()
        make_file(os.path.join(self.src, "a.bin"), 10_000, b"A")
        with self.assertRaises(SourceChangedError) as ctx:
            create_archive(self.src, self.archive)
        self.assertEqual(ctx.exception.modified, ["a.bin"])

    def test_touch_only_change_is_accepted(self):
        # 仅 mtime 变化、内容不变：重算摘要一致，放行并更新快照
        self._crash_after_first_file()
        os.utime(os.path.join(self.src, "a.bin"), (1_700_000_000, 1_700_000_000))
        manifest = create_archive(self.src, self.archive)
        self.assertEqual(manifest["totals"]["files"], 2)
        self.assertTrue(verify_archive(self.archive)["ok"])

    def test_full_check_catches_silent_modification(self):
        # 已完成条目内容被改、但 size+mtime 被伪装回去：--full-check 必须能抓到
        self._crash_after_first_file()
        path = os.path.join(self.src, "a.bin")
        st = os.stat(path)
        with open(path, "r+b") as f:
            f.write(b"CORRUPTED")
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        with self.assertRaises(SourceChangedError) as ctx:
            create_archive(self.src, self.archive, full_check=True)
        self.assertEqual(ctx.exception.modified, ["a.bin"])


class TestVerify(ArchiverTestBase):
    def test_verify_detects_corruption(self):
        make_file(os.path.join(self.src, "x.bin"), 100_000, b"x")
        create_archive(self.src, self.archive)
        with open(self.archive, "r+b") as f:
            f.seek(8192)  # 落在文件数据区（避开 tar 头）
            b = f.read(1)
            f.seek(8192)
            f.write(bytes([b[0] ^ 0xFF]))
        with self.assertRaises(ArchiveError):
            verify_archive(self.archive)

    def test_verify_rejects_archive_without_manifest(self):
        make_file(os.path.join(self.src, "x.bin"), 100, b"x")
        with tarfile.open(self.archive, "w") as tar:
            tar.add(os.path.join(self.src, "x.bin"), arcname="x.bin")
        with self.assertRaises(ArchiveError):
            verify_archive(self.archive)

    def test_manifest_totals(self):
        make_file(os.path.join(self.src, "f1"), 100, b"1")
        make_file(os.path.join(self.src, "sub/f2"), 200, b"2")
        manifest = create_archive(self.src, self.archive)
        t = manifest["totals"]
        self.assertEqual(t["files"], 2)
        self.assertEqual(t["total_size"], 300)
        self.assertEqual(t["entries"], 3)  # 2 文件 + 1 目录
        report = verify_archive(self.archive)
        self.assertEqual(report["total_size"], 300)
        self.assertEqual(report["entries"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
