"""End-to-end rotation demo: progress, interruption, resume, safe retirement.

Run:  python3 demo.py
"""

import os
import tempfile

import keywrap
from keywrap import KeyStore, RecordStore


def show_versions(store, label):
    counts = keywrap.count_by_version(store)
    pretty = ", ".join(f"v{v} x{n}" for v, n in sorted(counts.items()))
    print(f"  {label}: {pretty}")


def main():
    tmp = tempfile.mkdtemp(prefix="keywrap-demo-")
    ks = KeyStore(os.path.join(tmp, "keys.json"))
    store = RecordStore(os.path.join(tmp, "records"))

    v1 = ks.create_key()
    print(f"1) 初始：创建密钥 v{v1}，写入 50 条记录")
    for i in range(50):
        store.put(f"rec-{i:03d}", keywrap.wrap(f"payload-{i}".encode(), ks))
    show_versions(store, "版本分布")

    v2 = keywrap.begin_rotation(ks)
    print(f"\n2) 轮换阶段一：新密钥 v{v2} 已激活（新写入用 v{v2}，旧数据仍可解）")
    print(f"   抽查 rec-000 -> {keywrap.unwrap(store.get('rec-000'), ks)!r}")

    print("\n3) 轮换阶段二：重加密，模拟在第 20 条后崩溃")
    stop = {"n": 0}
    def should_stop():
        stop["n"] += 1
        return stop["n"] > 20
    def progress(stats, done, rid):
        pct = 100.0 * done / stats.total
        print(f"\r   进度 {done}/{stats.total} ({pct:5.1f}%) 最近处理 {rid}   ",
              end="", flush=True)
    stats = keywrap.rotate(store, ks, should_stop=should_stop, on_progress=progress)
    print(f"\n   {stats.summary(v2)}")
    show_versions(store, "中断点版本分布")
    print(f"   中断后抽查 rec-000 -> {keywrap.unwrap(store.get('rec-000'), ks)!r}")
    print(f"   中断后抽查 rec-049 -> {keywrap.unwrap(store.get('rec-049'), ks)!r}")
    left = keywrap.remaining_on_version(store, v1)
    print(f"   v{v1} 仍有 {left} 条引用 -> 不能下线旧密钥")

    print("\n4) 恢复：重新执行 rotate，自动跳过已迁移记录")
    stats = keywrap.rotate(store, ks, on_progress=progress)
    print(f"\n   {stats.summary(v2)}")
    show_versions(store, "完成后版本分布")

    print("\n5) 轮换阶段三：确认无引用后安全下线旧密钥")
    left = keywrap.remaining_on_version(store, v1)
    print(f"   remaining_on_version(v{v1}) = {left}")
    keywrap.retire_key(ks, store, v1)
    print(f"   v{v1} 已下线，当前密钥版本：{ks.versions()}")
    print(f"   下线后抽查 rec-000 -> {keywrap.unwrap(store.get('rec-000'), ks)!r}")


if __name__ == "__main__":
    main()
