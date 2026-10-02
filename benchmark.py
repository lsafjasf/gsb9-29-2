#!/usr/bin/env python3
"""续传 vs 全量重跑的耗时对比。

场景：构造一棵 --size-mb 大小的目录树，第一次运行在写入约 --crash-at 比例处
模拟崩溃，然后分别比较两种恢复策略的总耗时：

  * 全量重跑（无断点续传时的唯一选择）= 崩溃前耗时 + 从头完整再跑一遍
  * 断点续传                          = 崩溃前耗时 + 从断点继续到完成

用法: python3 benchmark.py [--size-mb 512] [--files 64] [--crash-at 0.5]
"""

import argparse
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import resumable_archive as ra


class SimulatedCrash(Exception):
    pass


def build_tree(root, total_bytes, nfiles):
    os.makedirs(os.path.join(root, "data", "sub-a"))
    os.makedirs(os.path.join(root, "data", "sub-b"))
    os.makedirs(os.path.join(root, "empty-dir"))
    per_file = max(1, total_bytes // nfiles)
    chunk = os.urandom(1024 * 1024)
    for i in range(nfiles):
        sub = "sub-a" if i % 2 else "sub-b"
        path = os.path.join(root, "data", sub, "blob-%04d.bin" % i)
        remaining = per_file
        with open(path, "wb") as fh:
            while remaining > 0:
                piece = chunk[: min(len(chunk), remaining)]
                fh.write(piece)
                remaining -= len(piece)


def run(args):
    tmp = tempfile.mkdtemp(prefix="rarc-bench-")
    try:
        src = os.path.join(tmp, "src")
        total_bytes = args.size_mb * 1024 * 1024
        print("构造测试数据: %d MiB / %d 个文件 ..." % (args.size_mb, args.files))
        build_tree(src, total_bytes, args.files)

        # ---- 基线：一次性全量归档 ----
        full_dst = os.path.join(tmp, "full.rarc")
        t0 = time.monotonic()
        stats = ra.create_archive(src, full_dst)
        t_full = time.monotonic() - t0
        ra.verify_archive(full_dst)

        # ---- 场景：中途崩溃 ----
        crash_dst = os.path.join(tmp, "crash.rarc")
        budget = [int(total_bytes * args.crash_at)]
        seen = [0]

        def hook(nbytes):
            seen[0] += nbytes
            if seen[0] > budget[0]:
                raise SimulatedCrash()

        t0 = time.monotonic()
        try:
            ra.create_archive(src, crash_dst, abort_hook=hook)
        except SimulatedCrash:
            pass
        t_before_crash = time.monotonic() - t0

        # 策略 A：全量重跑（删掉重来）
        restart_dst = os.path.join(tmp, "restart.rarc")
        t0 = time.monotonic()
        ra.create_archive(src, restart_dst, fresh=True)
        t_rerun = time.monotonic() - t0
        ra.verify_archive(restart_dst)
        total_restart = t_before_crash + t_rerun

        # 策略 B：断点续传
        t0 = time.monotonic()
        ra.create_archive(src, crash_dst)
        t_resume = time.monotonic() - t0
        ra.verify_archive(crash_dst, state_path=ra.default_state_path(crash_dst))
        total_resume = t_before_crash + t_resume

        saved = total_restart - total_resume
        pct = (saved / total_restart * 100) if total_restart else 0.0

        print()
        print("数据量:            %d MiB (%d 个文件, 崩溃点 %.0f%%)"
              % (args.size_mb, args.files, args.crash_at * 100))
        print("一次性全量归档:    %8.2f s" % t_full)
        print("崩溃前已耗时:      %8.2f s" % t_before_crash)
        print("全量重跑总耗时:    %8.2f s  (崩溃前 %.2f + 重跑 %.2f)"
              % (total_restart, t_before_crash, t_rerun))
        print("断点续传总耗时:    %8.2f s  (崩溃前 %.2f + 续传 %.2f)"
              % (total_resume, t_before_crash, t_resume))
        print("续传节省:          %8.2f s  (%.1f%%)" % (saved, pct))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="断点续传 vs 全量重跑耗时对比")
    parser.add_argument("--size-mb", type=int, default=512, help="测试数据总大小 (MiB)")
    parser.add_argument("--files", type=int, default=64, help="文件个数")
    parser.add_argument("--crash-at", type=float, default=0.5, help="崩溃点 (已写字节比例)")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
