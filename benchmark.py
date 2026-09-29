"""Benchmark: STM (optimistic validation + retry) vs explicit sorted
per-key locks. Standard library only. Run: python3 benchmark.py

Metrics per scenario (aggregated over ROUNDS rounds):
  total_txns, elapsed_s, throughput_tps
  conflicts / retries / conflict_rate_pct (STM; locks block instead)
  commit critical section latency: p50 / p95 / max (us)
  lock wait latency for the lock baseline: p50 / p95 / max (us)

Note: CPython's GIL serializes bytecode execution, so pure-register
transactions rarely interleave; the 'yield' variants insert a short sleep
between read and commit to expose what multi-core / IO-bearing workloads
look like.
"""

import json
import threading
import time

from stm import STM

THREADS = 8
ROUNDS = 3
DISJOINT_ITERS = 400
TRANSFER_ITERS = 300
ACCOUNTS = 16


def percentile(data, p):
    if not data:
        return 0.0
    ordered = sorted(data)
    idx = min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))
    return ordered[idx]


class LockStore:
    def __init__(self):
        self._data = {}
        self._guard = threading.Lock()
        self._locks = {}
        self.wait_times = []
        self.hold_times = []

    def _lock_for(self, key):
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def update(self, keys, fn):
        ordered = sorted(set(keys))
        t0 = time.perf_counter()
        acquired = [self._lock_for(k) for k in ordered]
        for lock in acquired:
            lock.acquire()
        t1 = time.perf_counter()
        try:
            fn(self)
        finally:
            for lock in reversed(acquired):
                lock.release()
            t2 = time.perf_counter()
        self.wait_times.append(t1 - t0)
        self.hold_times.append(t2 - t1)

    def get(self, key):
        return self._data.get(key, 0)

    def set(self, key, value):
        self._data[key] = value


def run_threads(target, threads=THREADS):
    ths = [threading.Thread(target=target) for _ in range(threads)]
    t0 = time.perf_counter()
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return time.perf_counter() - t0


def stm_measure(name, setup, body, iters, rounds=ROUNDS):
    stm = STM()
    setup(stm)
    stm.reset_stats()
    elapsed = 0.0
    for _ in range(rounds):
        elapsed += run_threads(
            lambda: [stm.run(body, retries=100000) for _ in range(iters)])
    s = stm.stats()
    total = THREADS * iters * rounds
    lats = s["commit_latency"]
    return {
        "scenario": name,
        "scheme": "STM",
        "rounds": rounds,
        "total_txns": total,
        "elapsed_s": round(elapsed, 4),
        "throughput_tps": round(total / elapsed, 1),
        "conflicts": s["conflicts"],
        "retries": s["retries"],
        "conflict_rate_pct": round(100 * s["conflict_rate"], 2),
        "commit_p50_us": round(lats["p50"] * 1e6, 2),
        "commit_p95_us": round(lats["p95"] * 1e6, 2),
        "commit_max_us": round(lats["max"] * 1e6, 2),
    }, stm


def locks_measure(name, keys_fn, mutate, iters, rounds=ROUNDS):
    store = LockStore()
    for i in range(ACCOUNTS):
        store.set("acc%d" % i, 1000)
    store.set("hot", 0)
    elapsed = 0.0

    def one_txn():
        keys = keys_fn()
        store.update(keys, mutate)

    for _ in range(rounds):
        elapsed += run_threads(lambda: [one_txn() for _ in range(iters)])
    total = THREADS * iters * rounds
    return {
        "scenario": name,
        "scheme": "locks",
        "rounds": rounds,
        "total_txns": total,
        "elapsed_s": round(elapsed, 4),
        "throughput_tps": round(total / elapsed, 1),
        "conflicts": 0,
        "retries": 0,
        "conflict_rate_pct": 0.0,
        "lock_wait_p50_us": round(percentile(store.wait_times, 50) * 1e6, 2),
        "lock_wait_p95_us": round(percentile(store.wait_times, 95) * 1e6, 2),
        "lock_wait_max_us": round(percentile(store.wait_times, 100) * 1e6, 2),
        "hold_p50_us": round(percentile(store.hold_times, 50) * 1e6, 2),
        "hold_p95_us": round(percentile(store.hold_times, 95) * 1e6, 2),
    }, store


def main():
    results = []

    # 1. single-thread tight loop: raw per-transaction overhead
    single_stm = STM()
    single_stm.run(lambda tx: tx.set("hot", 0))
    single_stm.reset_stats()
    t0 = time.perf_counter()
    n_single = 10000
    for _ in range(n_single):
        single_stm.run(lambda tx: tx.set("hot", tx.get("hot") + 1))
    elapsed = time.perf_counter() - t0
    s = single_stm.stats()
    results.append({
        "scenario": "single_thread",
        "scheme": "STM",
        "rounds": 1,
        "total_txns": n_single,
        "elapsed_s": round(elapsed, 4),
        "throughput_tps": round(n_single / elapsed, 1),
        "conflicts": 0, "retries": 0, "conflict_rate_pct": 0.0,
        "commit_p50_us": round(s["commit_latency"]["p50"] * 1e6, 2),
        "commit_p95_us": round(s["commit_latency"]["p95"] * 1e6, 2),
        "commit_max_us": round(s["commit_latency"]["max"] * 1e6, 2),
    })

    # 2. disjoint keys, high concurrency -> zero conflicts
    res, _ = stm_measure(
        "disjoint",
        lambda stm: None,
        (lambda tx: (lambda key: tx.set(key, tx.get(key, 0) + 1))(
            "k%d" % threading.get_ident())),
        DISJOINT_ITERS)
    results.append(res)

    # 3a. tight hotspot (GIL-serialized bytecode -> near-zero aborts)
    res, stm_hot = stm_measure(
        "hotspot_tight",
        lambda stm: stm.run(lambda tx: tx.set("hot", 0)),
        lambda tx: tx.set("hot", tx.get("hot") + 1),
        300)
    results.append(res)

    # 3b. hotspot with 100us yield between read and commit
    yield_s = 0.0001

    def rmw_yield(tx):
        value = tx.get("hot")
        time.sleep(yield_s)
        tx.set("hot", value + 1)

    res, stm_hot2 = stm_measure(
        "hotspot_yield100us",
        lambda stm: stm.run(lambda tx: tx.set("hot", 0)),
        rmw_yield, 100)
    results.append(res)

    # 3c. hotspot with 2ms yield -> severe contention / abort storm
    yield_long = 0.002

    def rmw_long(tx):
        value = tx.get("hot")
        time.sleep(yield_long)
        tx.set("hot", value + 1)

    res, stm_hot3 = stm_measure(
        "hotspot_yield2ms",
        lambda stm: stm.run(lambda tx: tx.set("hot", 0)),
        rmw_long, 25)
    results.append(res)

    # lock baselines at identical yields
    def lock_rmw(s):
        s.set("hot", s.get("hot") + 1)

    res, _ = locks_measure("hotspot_tight", lambda: ["hot"], lock_rmw, 300)
    results.append(res)

    def lock_rmw_yield(s):
        s.set("hot", s.get("hot") + 1)
        time.sleep(yield_s)

    res, _ = locks_measure("hotspot_yield100us", lambda: ["hot"],
                           lock_rmw_yield, 100)
    results.append(res)

    def lock_rmw_long(s):
        s.set("hot", s.get("hot") + 1)
        time.sleep(yield_long)

    res, lock_hot3 = locks_measure("hotspot_yield2ms", lambda: ["hot"],
                                   lock_rmw_long, 25)
    results.append(res)

    # 4. multi-key transfers
    def seed(stm):
        stm.run(lambda tx: [tx.set("acc%d" % i, 1000)
                            for i in range(ACCOUNTS)])

    def transfer(tx):
        src, dst = "acc3", "acc9"
        value = tx.get(src)
        time.sleep(0.00005)
        if value >= 1:
            tx.set(src, value - 1)
            tx.set(dst, tx.get(dst) + 1)

    res, stm_tf = stm_measure("transfer", seed, transfer, TRANSFER_ITERS)
    results.append(res)
    invariant_stm = stm_tf.get_committed("acc3") + stm_tf.get_committed("acc9")

    def lock_transfer_mutate(s):
        if s.get("acc3") >= 1:
            s.set("acc3", s.get("acc3") - 1)
            s.set("acc9", s.get("acc9") + 1)
            time.sleep(0.00005)

    res, lock_tf = locks_measure("transfer", lambda: ["acc3", "acc9"],
                                 lock_transfer_mutate, TRANSFER_ITERS)
    results.append(res)
    invariant_locks = lock_tf.get("acc3") + lock_tf.get("acc9")

    print(json.dumps(results, indent=2))
    print()
    print("invariant acc3+acc9: STM=%d locks=%d (expected 2000)"
          % (invariant_stm, invariant_locks))
    with open("benchmark_results.json", "w") as f:
        json.dump({"results": results,
                   "invariant": {"stm": invariant_stm,
                                 "locks": invariant_locks}}, f, indent=2)


if __name__ == "__main__":
    main()
