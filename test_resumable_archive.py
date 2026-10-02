#!/usr/bin/env python3
"""resumable_archive 的完整性与断点续传自测（仅标准库，unittest）。"""

import hashlib
import json
import os
import shutil
import tempfile
import unittest

import resumable_archive as ra


class SimulatedCrash(Exception):
    """模拟归档中途崩溃。"""


def make_crash_hook(byte_limit):
    """累计写入超过 byte_limit 字节后抛出 SimulatedCrash。"""
    seen = [0]

    def hook(nbytes):
        seen[0] += nbytes
        if seen[0] > byte_limit:
            raise SimulatedCrash("simulated crash after %d bytes" % seen[0])

    return hook


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rarc-test-")
        self.src = os.path.join(self.tmp, "src")
        os.makedirs(self.src)
        self.dst = os.path.join(self.tmp, "out.rarc")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_file(self, rel, data):
        path = os.path.join(self.src, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def make_tree(self, files=8, file_size=256 * 1024):
        """构造一棵带空目录、嵌套目录和多文件的测试树，返回总字节数。"""
        total = 0
        os.makedirs(os.path.join(self.src, "empty-dir"))
        os.makedirs(os.path.join(self.src, "nested", "deep"))
        for i in range(files):
            sub = "nested" if i % 2 else "."
            rel = os.path.join(sub, "file-%02d.bin" % i) if sub != "." else "file-%02d.bin" % i
            data = os.urandom(file_size)
            self.write_file(rel, data)
            total += len(data)
        return total


class TestBasicArchive(Base):
    def test_empty_directory(self):
        stats = ra.create_archive(self.src, self.dst)
        self.assertEqual(stats["entries"], 0)
        result = ra.verify_archive(self.dst)
        self.assertEqual(result["entries"], 0)
        self.assertEqual(result["total_data_bytes"], 0)

    def test_directory_with_only_empty_subdirs(self):
        os.makedirs(os.path.join(self.src, "a", "b"))
        os.makedirs(os.path.join(self.src, "c"))
        stats = ra.create_archive(self.src, self.dst)
        self.assertEqual(stats["entries"], 3)  # a, a/b, c
        self.assertEqual(ra.verify_archive(self.dst)["entries"], 3)

    def test_single_file(self):
        data = os.urandom(1024 * 1024 + 7)
        self.write_file("only.bin", data)
        stats = ra.create_archive(self.src, self.dst)
        self.assertEqual(stats["files"], 1)
        self.assertEqual(stats["total_data_bytes"], len(data))
        result = ra.verify_archive(self.dst)
        self.assertEqual(result["files"], 1)

    def test_nested_tree_roundtrip(self):
        total = self.make_tree()
        stats = ra.create_archive(self.src, self.dst)
        self.assertEqual(stats["total_data_bytes"], total)
        result = ra.verify_archive(self.dst, state_path=ra.default_state_path(self.dst))
        self.assertEqual(result["entries"], stats["entries"])

    def test_verify_detects_corruption(self):
        self.make_tree(files=3, file_size=64 * 1024)
        ra.create_archive(self.src, self.dst)
        # 翻转第一个文件数据区（跳过 MAGIC 与第一个条目头）中的一个字节
        with open(self.dst, "rb") as fh:
            fh.seek(len(ra.MAGIC))
            (hlen,) = ra._LEN.unpack(fh.read(ra._LEN.size))
            data_pos = len(ra.MAGIC) + ra._LEN.size + hlen
        with open(self.dst, "r+b") as fh:
            fh.seek(data_pos + 100)
            byte = fh.read(1)
            fh.seek(-1, os.SEEK_CUR)
            fh.write(bytes([byte[0] ^ 0xFF]))
        with self.assertRaises(ra.ArchiveError):
            ra.verify_archive(self.dst)

    def test_verify_detects_truncation(self):
        self.make_tree(files=2, file_size=64 * 1024)
        ra.create_archive(self.src, self.dst)
        size = os.path.getsize(self.dst)
        with open(self.dst, "r+b") as fh:
            fh.truncate(size - 100)
        with self.assertRaises(ra.ArchiveError):
            ra.verify_archive(self.dst)


class TestResume(Base):
    def test_resume_after_mid_entry_interrupt(self):
        """中断在某个文件的写入中间，续传结果须与一次性全量归档逐字节一致。"""
        total = self.make_tree(files=6, file_size=512 * 1024)

        # 一次性全量归档（对照组）
        ref_dst = os.path.join(self.tmp, "ref.rarc")
        ra.create_archive(self.src, ref_dst)

        # 第一次运行：写到一半崩溃
        with self.assertRaises(SimulatedCrash):
            ra.create_archive(self.src, self.dst, abort_hook=make_crash_hook(total // 2))

        state = ra._load_state(ra.default_state_path(self.dst))
        self.assertFalse(state["done"])
        self.assertGreater(os.path.getsize(self.dst), state["archive_offset"],
                           "崩溃时归档里应残留未写完的半个条目")
        completed_before = dict(state["completed"])
        self.assertTrue(completed_before)

        # 续传
        stats = ra.create_archive(self.src, self.dst)
        self.assertTrue(stats["resumed"])

        # 与全量归档逐字节一致，且通过完整性校验
        self.assertEqual(sha256_of(self.dst), sha256_of(ref_dst))
        ra.verify_archive(self.dst, state_path=ra.default_state_path(self.dst))

        # 已完成条目确实被跳过：续传不应重写已完成的文件
        state2 = ra._load_state(ra.default_state_path(self.dst))
        for rel, info in completed_before.items():
            self.assertEqual(state2["completed"][rel], info)

    def test_resume_between_entries(self):
        """恰好在条目边界崩溃（写完 0 字节数据前崩溃）也能续传。"""
        self.make_tree(files=4, file_size=128 * 1024)
        calls = [0]

        def hook(nbytes):
            calls[0] += 1
            if calls[0] >= 2:  # 第二个条目刚开始就崩
                raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            ra.create_archive(self.src, self.dst, abort_hook=hook)
        stats = ra.create_archive(self.src, self.dst)
        self.assertTrue(stats["resumed"])
        ra.verify_archive(self.dst)

    def test_completed_run_refuses_to_continue(self):
        self.make_tree(files=2, file_size=32 * 1024)
        ra.create_archive(self.src, self.dst)
        with self.assertRaises(ra.ArchiveError):
            ra.create_archive(self.src, self.dst)
        # --fresh 可以重来
        stats = ra.create_archive(self.src, self.dst, fresh=True)
        self.assertFalse(stats["resumed"])

    def test_missing_archive_with_state(self):
        self.make_tree(files=2, file_size=32 * 1024)
        with self.assertRaises(SimulatedCrash):
            ra.create_archive(self.src, self.dst, abort_hook=make_crash_hook(10))
        os.remove(self.dst)
        with self.assertRaises(ra.ArchiveError):
            ra.create_archive(self.src, self.dst)


class TestSourceChangeDetection(Base):
    def _crash_once(self, **kwargs):
        self.make_tree(**kwargs)
        with self.assertRaises(SimulatedCrash):
            ra.create_archive(self.src, self.dst, abort_hook=make_crash_hook(64 * 1024))

    def test_modified_file_detected(self):
        self._crash_once(files=4, file_size=64 * 1024)
        self.write_file("file-00.bin", b"x" * 999999)  # 修改已有文件
        with self.assertRaises(ra.SourceChangedError) as ctx:
            ra.create_archive(self.src, self.dst)
        self.assertEqual(ctx.exception.modified, ["file-00.bin"])

    def test_added_file_detected(self):
        self._crash_once(files=4, file_size=64 * 1024)
        self.write_file("brand-new.bin", b"new")
        with self.assertRaises(ra.SourceChangedError) as ctx:
            ra.create_archive(self.src, self.dst)
        self.assertEqual(ctx.exception.added, ["brand-new.bin"])

    def test_deleted_file_detected(self):
        self._crash_once(files=4, file_size=64 * 1024)
        os.remove(os.path.join(self.src, "nested", "file-01.bin"))
        with self.assertRaises(ra.SourceChangedError) as ctx:
            ra.create_archive(self.src, self.dst)
        self.assertEqual(ctx.exception.removed, ["nested/file-01.bin"])

    def test_same_size_same_mtime_edit_detected_with_verify_hashes(self):
        """篡改已完成文件但保持 size+mtime 不变：--verify-hashes 必须能抓住。"""
        self.make_tree(files=4, file_size=256 * 1024)
        # 崩溃点设在 600 KiB：file-00(256K)、nested/file-01(256K) 已完成，崩在 file-02 中
        with self.assertRaises(SimulatedCrash):
            ra.create_archive(self.src, self.dst, abort_hook=make_crash_hook(600 * 1024))
        completed = ra._load_state(ra.default_state_path(self.dst))["completed"]
        self.assertIn("file-00.bin", completed)

        target = os.path.join(self.src, "file-00.bin")
        st = os.stat(target)
        with open(target, "r+b") as fh:
            fh.write(b"\xAA" * 4096)  # 改内容，不改大小
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))  # 连 mtime 也恢复

        # stat 快照看不出变化，但逐条目 sha256 复验必然失败
        with self.assertRaises(ra.ArchiveError):
            ra.create_archive(self.src, self.dst, verify_hashes=True)

    def test_fresh_after_change_starts_over(self):
        self._crash_once(files=4, file_size=64 * 1024)
        self.write_file("extra.bin", b"extra")
        with self.assertRaises(ra.SourceChangedError):
            ra.create_archive(self.src, self.dst)
        stats = ra.create_archive(self.src, self.dst, fresh=True)
        self.assertFalse(stats["resumed"])
        ra.verify_archive(self.dst)


class TestCli(Base):
    def test_cli_create_and_verify(self):
        self.make_tree(files=3, file_size=32 * 1024)
        self.assertEqual(ra.main(["create", self.src, self.dst]), 0)
        self.assertEqual(ra.main(["verify", self.dst]), 0)

    def test_cli_reports_source_change(self):
        self.make_tree(files=3, file_size=64 * 1024)
        with self.assertRaises(SimulatedCrash):
            ra.create_archive(self.src, self.dst, abort_hook=make_crash_hook(10))
        self.write_file("changed.bin", b"z" * 12345)
        self.assertEqual(ra.main(["create", self.src, self.dst]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
