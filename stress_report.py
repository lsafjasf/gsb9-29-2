"""压力与公平性报告：打印等待时长分布，验证 FIFO 与不变量。

运行：python3 stress_report.py
"""

from __future__ import annotations

import threading
import time

from fair_sync import FairBarrier, FairSemaphore


def percentile(sorted_data, pct):
    if not sorted_data:
        return 0.0
    k = (len(sorted_data) - 1) * pct / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_data) - 1)
    return sorted_data[lo] + (sorted_data[hi] - sorted_data[lo]) * (k - lo)


def print_distribution(title, samples_ms):
    data = sorted(samples_ms)
    print(f"\n[{title}] 样本数 = {len(data)}")
    print(f"  min   : {data[0]:9.3f} ms")
    print(f"  p50   : {percentile(data, 50):9.3f} ms")
    print(f"  p90   : {percentile(data, 90):9.3f} ms")
    print(f"  p95   : {percentile(data, 95):9.3f} ms")
    print(f"  p99   : {percentile(data, 99):9.3f} ms")
    print(f"  max   : {data[-1]:9.3f} ms")
    print(f"  mean  : {sum(data) / len(data):9.3f} ms")


def semaphore_wait_distribution():
    """32 线程竞争 4 个许可，统计每次 acquire 的排队等待时长。"""
    capacity, n_threads, iterations = 4, 32, 300
    sem = FairSemaphore(capacity)
    waits = []                       # 全部样本（毫秒）
    per_thread_total = [0.0] * n_threads
    per_thread_max = [0.0] * n_threads
    lock = threading.Lock()
    errors = []

    def worker(tid):
        local_waits = []
        try:
            for _ in range(iterations):
                t0 = time.monotonic()
                if not sem.acquire(timeout=30):
                    raise AssertionError("意外超时（疑似饿死）")
                waited = (time.monotonic() - t0) * 1000.0
                local_waits.append(waited)
                sem.check_invariants()
                time.sleep(0.0003)   # 模拟临界区内的工作
                sem.release()
        except Exception as exc:     # pragma: no cover
            errors.append(exc)
        with lock:
            waits.extend(local_waits)
            per_thread_total[tid] = sum(local_waits)
            per_thread_max[tid] = max(local_waits)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - start

    assert not errors, errors
    print(f"\n=== 信号量等待分布（capacity={capacity}, threads={n_threads}, "
          f"iterations={iterations}，总耗时 {elapsed:.2f}s）===")
    print_distribution("acquire 排队等待时长", waits)
    print("\n  公平性（无饿死证据）：")
    print(f"  每线程总等待   min/max : {min(per_thread_total):9.1f} / "
          f"{max(per_thread_total):9.1f} ms "
          f"(比值 {max(per_thread_total) / max(min(per_thread_total), 1e-9):.2f})")
    print(f"  每线程最大单次 min/max : {min(per_thread_max):9.3f} / "
          f"{max(per_thread_max):9.3f} ms")
    assert sem.held == 0 and sem.available == capacity and sem.waiting == 0
    sem.check_invariants()


def semaphore_fifo_verification():
    """capacity=1，错开到达，验证授予顺序严格等于入队顺序（FIFO）。"""
    n = 16
    sem = FairSemaphore(1)
    sem.acquire()
    grant_order = []
    lock = threading.Lock()
    gate = threading.Event()

    def worker(i):
        gate.wait()
        time.sleep(0.01 * i)   # 到达顺序 = 线程编号
        sem.acquire()
        with lock:
            grant_order.append(i)
        sem.release()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    gate.set()
    time.sleep(0.01 * n + 0.2)
    sem.release()
    for t in threads:
        t.join()

    print("\n=== 信号量 FIFO 验证（capacity=1，错开到达）===")
    print(f"  入队顺序: {list(range(n))}")
    print(f"  授予顺序: {grant_order}")
    ok = grant_order == list(range(n))
    print(f"  严格 FIFO: {'通过' if ok else '失败'}")
    assert ok, "FIFO 被违反"


def barrier_release_report():
    """16 个参与方复用 500 轮，验证每轮恰好放行一次且全部到齐才放行。"""
    parties, rounds = 16, 500
    barrier = FairBarrier(parties)
    arrived = [0] * rounds
    passed = [0] * rounds
    spreads = []                 # 每轮放行时刻的最大离散度
    round_release_time = [None] * rounds
    lock = threading.Lock()
    errors = []

    def worker():
        try:
            for r in range(rounds):
                with lock:
                    arrived[r] += 1
                barrier.wait()
                now = time.monotonic()
                with lock:
                    assert arrived[r] == parties, "未集齐就放行！"
                    passed[r] += 1
                    if round_release_time[r] is None:
                        round_release_time[r] = [now, now]
                    else:
                        slot = round_release_time[r]
                        slot[0] = min(slot[0], now)
                        slot[1] = max(slot[1], now)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(parties)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert passed == [parties] * rounds, "存在重复放行或丢失唤醒！"
    assert barrier.generation == rounds, "generation 计数与轮数不符！"
    spreads = [(slot[1] - slot[0]) * 1000.0 for slot in round_release_time]
    print(f"\n=== 屏障放行报告（parties={parties}, rounds={rounds}）===")
    print(f"  每轮放行人数: 全部 == {parties}（无重复放行，无丢失唤醒）")
    print(f"  generation 总数 == {rounds}（每轮恰好放行一次）")
    print_distribution("单轮内所有线程被唤醒的时间离散度", spreads)


if __name__ == "__main__":
    semaphore_wait_distribution()
    semaphore_fifo_verification()
    barrier_release_report()
    print("\n全部压力检查通过。")
