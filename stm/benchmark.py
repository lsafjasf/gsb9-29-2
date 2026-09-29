"""STM 与加锁方案的对比基准。

场景:
1. no-conflict  各线程操作互不相交的账户对 (无冲突并发)
2. hotspot      所有转账都经过账户 0 (高冲突热点)

输出: 吞吐、总耗时; STM 额外输出冲突率、平均重试次数、提交延迟 avg/p99。
两种方案都校验账户总额不变, 保证结果正确。
"""
import random
import threading
import time

import stm

ACCOUNTS = 64
THREADS = 8
OPS_PER_THREAD = 2000
INIT_BALANCE = 1000


# ---------- STM 方案 ----------

def make_stm_accounts():
    return [stm.TVar(INIT_BALANCE) for _ in range(ACCOUNTS)]


def stm_transfer(tvars, i, j, amount, work_s=0.0):
    def body(tx):
        bi = tx.read(tvars[i])
        bj = tx.read(tvars[j])
        if work_s:
            time.sleep(work_s)  # 模拟事务内业务耗时, 拉开读-提交窗口
        tx.write(tvars[i], bi - amount)
        tx.write(tvars[j], bj + amount)
    stm.atomically(body, max_retries=10_000)


# ---------- 加锁方案 (按账户序号排序加锁, 避免死锁) ----------

def make_lock_accounts():
    locks = [threading.Lock() for _ in range(ACCOUNTS)]
    balances = [INIT_BALANCE] * ACCOUNTS
    return balances, locks


def lock_transfer(balances, locks, i, j, amount, work_s=0.0):
    lo, hi = (i, j) if i < j else (j, i)
    with locks[lo]:
        with locks[hi]:
            if work_s:
                time.sleep(work_s)  # 同样的业务耗时, 公平对比
            balances[i] -= amount
            balances[j] += amount


# ---------- 场景驱动 ----------

def pick_pair(rng, hotspot, tid):
    if hotspot:
        # 所有线程都往账户 0 转账: 制造热点
        other = rng.randrange(1, ACCOUNTS)
        return 0, other
    # 每个线程只在自己专属的一段账户内转账: 线程间无交集
    span = ACCOUNTS // THREADS
    base = tid * span
    i = base + rng.randrange(span)
    j = base + rng.randrange(span)
    while j == i:
        j = base + rng.randrange(span)
    return i, j


def run_stm(hotspot, work_s=0.0):
    tvars = make_stm_accounts()
    stm.reset_stats()

    def worker(tid):
        rng = random.Random(tid)
        for _ in range(OPS_PER_THREAD):
            i, j = pick_pair(rng, hotspot, tid)
            stm_transfer(tvars, i, j, 1, work_s)

    start = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(t,))
               for t in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start

    total = sum(v.peek() for v in tvars)
    assert total == ACCOUNTS * INIT_BALANCE, "STM 总额不变量被破坏!"
    return elapsed, stm.get_stats()


def run_lock(hotspot, work_s=0.0):
    balances, locks = make_lock_accounts()

    def worker(tid):
        rng = random.Random(tid)
        for _ in range(OPS_PER_THREAD):
            i, j = pick_pair(rng, hotspot, tid)
            lock_transfer(balances, locks, i, j, 1, work_s)

    start = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(t,))
               for t in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start

    assert sum(balances) == ACCOUNTS * INIT_BALANCE, "锁方案总额不变量被破坏!"
    return elapsed


def report(name, stm_elapsed, stats, lock_elapsed):
    ops = THREADS * OPS_PER_THREAD
    print(f"\n=== 场景: {name} ({THREADS} 线程 x {OPS_PER_THREAD} 次转账) ===")
    print(f"{'方案':<10}{'总耗时(s)':>10}{'吞吐(ops/s)':>14}"
          f"{'冲突率':>10}{'平均重试':>10}{'提交延迟avg(ms)':>16}{'p99(ms)':>10}")
    print(f"{'STM':<10}{stm_elapsed:>10.3f}{ops / stm_elapsed:>14.0f}"
          f"{stats['conflict_rate']:>10.2%}"
          f"{stats['avg_retries_per_commit']:>10.2f}"
          f"{stats['commit_latency_avg_s'] * 1e3:>16.3f}"
          f"{stats['commit_latency_p99_s'] * 1e3:>10.3f}")
    print(f"{'细粒度锁':<10}{lock_elapsed:>10.3f}{ops / lock_elapsed:>14.0f}"
          f"{'-':>10}{'-':>10}{'-':>16}{'-':>10}")
    print(f"  STM 明细: commits={stats['commits']} "
          f"conflicts={stats['conflicts']} retries={stats['retries']}")


def main():
    print(f"账户数={ACCOUNTS}, 初始余额={INIT_BALANCE}, "
          f"两方案均校验转账总额守恒")

    stm_t, stm_s = run_stm(hotspot=False)
    lock_t = run_lock(hotspot=False)
    report("no-conflict (线程间账户互不相交)", stm_t, stm_s, lock_t)

    stm_t, stm_s = run_stm(hotspot=True, work_s=0.0001)
    lock_t = run_lock(hotspot=True, work_s=0.0001)
    report("hotspot (所有转账经过账户 0, 事务内含 0.1ms 业务耗时)",
           stm_t, stm_s, lock_t)


if __name__ == "__main__":
    main()
