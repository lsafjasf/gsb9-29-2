#!/usr/bin/env python3
"""断点续传 vs 从头再来 耗时对比。

场景：归档进行到约一半时进程被杀。
- 从头再来：已耗时 T1 作废，重新全量归档耗时 T_full，总计 T1 + T_full。
- 断点续传：已耗时 T1 + 续传剩余 T_resume，总计 T1 + T_resume。

用法: python3 bench.py [--mb 128] [--files 64] [--keep]
"""

import argparse
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import archiver
from archiver import create_archive, verify_archive


class SimulatedCrash(Exception):
    pass


def gen_tree(root, total_bytes, nfiles):
    os.makedirs(root)
    per = total_bytes // nfiles
    chunk = os.urandom(1024 * 1024)
    for i in range(nfiles):
        path = os.path.join(root, "dir%02d" % (i % 8), "file%04d.bin" % i)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        remaining = per
        with open(path, "wb") as f:
            while remaining > 0:
                n = min(remaining, len(chunk))
                f.write(chunk[:n])
                remaining -= n


def timed_create(src, archive, **kw):
    t0 = time.monotonic()
    manifest = create_archive(src, archive, **kw)
    return time.monotonic() - t0, manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mb", type=int, default=128, help="数据总量 MiB（默认 128）")
    ap.add_argument("--files", type=int, default=64, help="文件个数（默认 64）")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="archiver-bench-")
    src = os.path.join(tmp, "src")
    arc_full = os.path.join(tmp, "full.tar")
    arc_resume = os.path.join(tmp, "resume.tar")

    print("生成测试数据: %d MiB / %d 个文件 ..." % (args.mb, args.files))
    gen_tree(src, args.mb * 1024 * 1024, args.files)

    # 1) 全量归档（无中断）
    t_full, m1 = timed_create(src, arc_full)
    verify_archive(arc_full)

    # 2) 归档到约一半时“宕机”
    half = [0]

    def entry_hook(rel):
        half[0] += 1
        if half[0] >= args.files // 2:
            raise SimulatedCrash()

    t0 = time.monotonic()
    try:
        create_archive(src, arc_resume, entry_hook=entry_hook)
    except SimulatedCrash:
        pass
    t_crash = time.monotonic() - t0
    state = archiver._load_state(arc_resume + ".state.json")
    done = len(state["completed"])
    print("模拟中断: 已完成 %d 个条目后进程被杀" % done)

    # 3a) 断点续传：接着跑完
    t_resume, m2 = timed_create(src, arc_resume)
    verify_archive(arc_resume)

    # 3b) 对照组：中断后从头再来
    t_restart, _ = timed_create(src, os.path.join(tmp, "restart.tar"))

    assert m1["totals"] == m2["totals"], "续传结果与全量不一致！"

    cost_restart = t_crash + t_restart
    cost_resume = t_crash + t_resume
    saved = cost_restart - cost_resume

    print()
    print("================ 耗时对比 ================")
    print("全量归档（一次成功）        : %7.2f s" % t_full)
    print("中断时已耗时（两种方案都浪费）: %7.2f s" % t_crash)
    print("中断后从头再来              : %7.2f s  (总计 %7.2f s)" % (t_restart, cost_restart))
    print("中断后续传剩余部分          : %7.2f s  (总计 %7.2f s)" % (t_resume, cost_resume))
    print("续传节省                    : %7.2f s  (%.1f%%)" % (
        saved, saved / cost_restart * 100 if cost_restart else 0))
    print("两次归档清单一致，完整性校验均通过。")

    if args.keep:
        print("临时目录保留在: %s" % tmp)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
