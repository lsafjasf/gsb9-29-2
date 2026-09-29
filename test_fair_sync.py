"""FairSemaphore / FairBarrier 的单元测试与并发压力测试。

运行：python3 -m unittest test_fair_sync -v
"""

from __future__ import annotations

import threading
import time
import unittest

from fair_sync import (
    BarrierTimeoutError,
    BrokenBarrierError,
    FairBarrier,
    FairSemaphore,
)


def run_threads(threads, join_timeout=30.0):
    for t in threads:
        t.start()
    for t in threads:
        t.join(join_timeout)
        assert not t.is_alive(), "线程未在限定时间内结束（疑似死锁/饿死）"


class ErrorCollector:
    """收集工作线程里的异常，供主线程统一断言。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.errors = []

    def record(self, exc):
        with self._lock:
            self.errors.append(exc)

    def assert_empty(self, testcase):
        testcase.assertEqual(self.errors, [])


class TestSemaphoreBasics(unittest.TestCase):
    def test_capacity_must_be_positive(self):
        for bad in (0, -1, -100):
            with self.assertRaises(ValueError):
                FairSemaphore(bad)

    def test_acquire_up_to_capacity_then_block(self):
        sem = FairSemaphore(2)
        self.assertTrue(sem.acquire(timeout=0))
        self.assertTrue(sem.acquire(timeout=0))
        self.assertFalse(sem.acquire(timeout=0.05))  # 第三个必须等待并超时
        sem.check_invariants()

    def test_release_count_validation(self):
        sem = FairSemaphore(2)
        with self.assertRaises(ValueError):
            sem.release()            # 未持有任何许可
        self.assertTrue(sem.acquire())
        with self.assertRaises(ValueError):
            sem.release(2)           # 只持有 1 个
        with self.assertRaises(ValueError):
            sem.release(0)
        with self.assertRaises(ValueError):
            sem.release(-3)
        sem.release()
        self.assertTrue(sem.acquire())
        self.assertTrue(sem.acquire())
        sem.release(2)               # 持有 2 个时允许一次释放 2 个
        sem.check_invariants()
        self.assertEqual(sem.held, 0)
        self.assertEqual(sem.available, 2)

    def test_negative_timeout_rejected(self):
        sem = FairSemaphore(1)
        with self.assertRaises(ValueError):
            sem.acquire(timeout=-0.1)

    def test_timeout_returns_false_and_waits(self):
        sem = FairSemaphore(1)
        self.assertTrue(sem.acquire())
        start = time.monotonic()
        self.assertFalse(sem.acquire(timeout=0.1))
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.09)  # 不能提前返回
        sem.release()


class TestSemaphoreTimeoutThenRelease(unittest.TestCase):
    """覆盖"超时后释放"：超时离开队列的等待者不得吞掉之后释放的许可。"""

    def test_permit_not_lost_after_waiter_timeout(self):
        sem = FairSemaphore(1)
        self.assertTrue(sem.acquire())                    # 主线程持有
        self.assertFalse(sem.acquire(timeout=0.05))       # 等待者超时离开
        sem.check_invariants()
        self.assertEqual(sem.waiting, 0)
        sem.release()                                     # 超时之后才释放
        self.assertTrue(sem.acquire(timeout=0.2))         # 许可必须可用
        sem.release()
        sem.check_invariants()

    def test_concurrent_timeout_then_release(self):
        sem = FairSemaphore(1)
        sem.acquire()
        collector = ErrorCollector()
        results = []

        def waiter():
            try:
                results.append(sem.acquire(timeout=0.05))  # 必然超时
            except Exception as exc:  # pragma: no cover
                collector.record(exc)

        threads = [threading.Thread(target=waiter) for _ in range(4)]
        run_threads(threads)
        collector.assert_empty(self)
        self.assertEqual(results, [False] * 4)
        self.assertEqual(sem.waiting, 0)
        sem.release()
        # 4 个等待者都超时后，许可一个都不能少
        for _ in range(4):
            self.assertTrue(sem.acquire(timeout=0.2))
            sem.release()
        sem.check_invariants()


class TestSemaphoreExceptionPath(unittest.TestCase):
    """异常路径退出：hold() 的 finally 必须释放许可。"""

    def test_exception_inside_hold_releases_permit(self):
        sem = FairSemaphore(1)
        with self.assertRaises(RuntimeError):
            with sem.hold():
                raise RuntimeError("boom")
        sem.check_invariants()
        self.assertEqual(sem.held, 0)
        self.assertTrue(sem.acquire(timeout=0.1))
        sem.release()

    def test_exception_in_worker_does_not_deadlock_others(self):
        sem = FairSemaphore(1)
        collector = ErrorCollector()
        done = []

        def bad_worker():
            try:
                with sem.hold():
                    raise RuntimeError("worker failed")
            except RuntimeError:
                pass
            except Exception as exc:  # pragma: no cover
                collector.record(exc)

        def good_worker():
            try:
                with sem.hold(timeout=5):
                    done.append(1)
            except Exception as exc:  # pragma: no cover
                collector.record(exc)

        threads = [threading.Thread(target=bad_worker)]
        threads += [threading.Thread(target=good_worker) for _ in range(3)]
        run_threads(threads)
        collector.assert_empty(self)
        self.assertEqual(len(done), 3)
        sem.check_invariants()


class TestSemaphoreFairness(unittest.TestCase):
    """FIFO：许可必须按入队顺序授予，不允许插队。"""

    def test_fifo_grant_order(self):
        n = 8
        sem = FairSemaphore(1)
        sem.acquire()  # 主线程先占住，迫使工作线程排队
        grant_order = []
        lock = threading.Lock()
        gate = threading.Event()

        def worker(i):
            gate.wait()                 # 统一放行，尽量同时竞争
            time.sleep(0.02 * i)        # 用递增延时确定入队顺序
            sem.acquire()
            with lock:
                grant_order.append(i)
            sem.release()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        gate.set()
        time.sleep(0.02 * n + 0.2)      # 等全部入队
        self.assertEqual(sem.waiting, n)
        sem.release()                   # 开始按 FIFO 依次授予
        for t in threads:
            t.join(10)
            self.assertFalse(t.is_alive())
        self.assertEqual(grant_order, list(range(n)))
        sem.check_invariants()


class TestSemaphoreStress(unittest.TestCase):
    """压力测试：任意时刻持有数不超过上限，许可守恒。"""

    def test_capacity_invariant_under_contention(self):
        capacity, n_threads, iterations = 3, 16, 300
        sem = FairSemaphore(capacity)
        collector = ErrorCollector()
        occupancy = 0
        max_occupancy = 0
        occ_lock = threading.Lock()

        def worker():
            nonlocal occupancy, max_occupancy
            try:
                for _ in range(iterations):
                    if not sem.acquire(timeout=10):
                        raise AssertionError("意外超时（疑似饿死）")
                    with occ_lock:
                        occupancy += 1
                        assert occupancy <= capacity, "持有数超过上限！"
                        max_occupancy = max(max_occupancy, occupancy)
                    sem.check_invariants()
                    time.sleep(0)  # 让出 CPU，制造交错
                    with occ_lock:
                        occupancy -= 1
                    sem.release()
            except Exception as exc:
                collector.record(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        run_threads(threads, join_timeout=60)
        collector.assert_empty(self)
        self.assertGreater(max_occupancy, 1)        # 确实发生了并发
        self.assertLessEqual(max_occupancy, capacity)
        self.assertEqual(occupancy, 0)
        self.assertEqual(sem.held, 0)
        self.assertEqual(sem.available, capacity)
        self.assertEqual(sem.waiting, 0)
        sem.check_invariants()

    def test_mixed_timeout_contention(self):
        """获取带随机超时、与释放激烈争用，结束后账目必须分毫不差。"""
        capacity, n_threads, iterations = 2, 12, 200
        sem = FairSemaphore(capacity)
        collector = ErrorCollector()

        def worker(seed):
            try:
                for i in range(iterations):
                    timeout = 0.001 * ((seed + i) % 4)  # 0~3ms，部分会超时
                    if sem.acquire(timeout=timeout):
                        sem.check_invariants()
                        sem.release()
            except Exception as exc:
                collector.record(exc)

        threads = [
            threading.Thread(target=worker, args=(s,)) for s in range(n_threads)
        ]
        run_threads(threads, join_timeout=60)
        collector.assert_empty(self)
        self.assertEqual(sem.held, 0)
        self.assertEqual(sem.available, capacity)
        self.assertEqual(sem.waiting, 0)
        sem.check_invariants()


class TestBarrierBasics(unittest.TestCase):
    def test_negative_parties_rejected(self):
        with self.assertRaises(ValueError):
            FairBarrier(-1)

    def test_zero_parties_returns_immediately(self):
        barrier = FairBarrier(0)
        start = time.monotonic()
        for _ in range(100):
            self.assertEqual(barrier.wait(), 0)
            self.assertEqual(barrier.wait(timeout=0.01), 0)
        self.assertLess(time.monotonic() - start, 1.0)
        barrier.check_invariants()

    def test_single_party(self):
        barrier = FairBarrier(1)
        self.assertEqual(barrier.wait(), 0)
        self.assertEqual(barrier.wait(), 0)  # 可复用

    def test_all_parties_released_together(self):
        parties = 6
        barrier = FairBarrier(parties)
        collector = ErrorCollector()
        released = []
        lock = threading.Lock()

        def worker():
            try:
                barrier.wait()
                with lock:
                    released.append(time.monotonic())
            except Exception as exc:  # pragma: no cover
                collector.record(exc)

        threads = [threading.Thread(target=worker) for _ in range(parties)]
        run_threads(threads)
        collector.assert_empty(self)
        self.assertEqual(len(released), parties)
        # 全部到齐才放行：放行时刻应非常集中
        self.assertLess(max(released) - min(released), 1.0)

    def test_negative_timeout_rejected(self):
        barrier = FairBarrier(2)
        with self.assertRaises(ValueError):
            barrier.wait(timeout=-1)


class TestBarrierTimeoutAndReset(unittest.TestCase):
    def test_timeout_breaks_barrier_and_reset_recovers(self):
        barrier = FairBarrier(3)
        outcomes = []
        lock = threading.Lock()

        def waiter():
            try:
                barrier.wait(timeout=0.1)
                with lock:
                    outcomes.append("passed")
            except BarrierTimeoutError:
                with lock:
                    outcomes.append("timeout")
            except BrokenBarrierError:
                with lock:
                    outcomes.append("broken")

        # 只来 2 个，第 3 个永远不到 => 一个超时打断，另一个收到 Broken
        threads = [threading.Thread(target=waiter) for _ in range(2)]
        run_threads(threads)
        self.assertEqual(sorted(outcomes), ["broken", "timeout"])
        self.assertTrue(barrier.broken)

        # broken 状态下新的 wait 立即抛 BrokenBarrierError
        with self.assertRaises(BrokenBarrierError):
            barrier.wait(timeout=1)

        barrier.reset()
        self.assertFalse(barrier.broken)
        # reset 后屏障恢复可用
        passed = []

        def passer():
            barrier.wait()
            passed.append(1)

        threads = [threading.Thread(target=passer) for _ in range(3)]
        run_threads(threads)
        self.assertEqual(len(passed), 3)

    def test_reset_with_waiting_threads(self):
        barrier = FairBarrier(2)
        outcomes = []

        def waiter():
            try:
                barrier.wait()
                outcomes.append("passed")
            except BrokenBarrierError:
                outcomes.append("broken")

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.1)  # 确保已进入等待
        barrier.reset()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(outcomes, ["broken"])


class TestBarrierExceptionPath(unittest.TestCase):
    """异常路径：参与者中途抛异常永不到达，其余等待者靠超时退出。"""

    def test_missing_participant_breaks_then_recovers(self):
        barrier = FairBarrier(3)
        collector = ErrorCollector()
        outcomes = []
        lock = threading.Lock()

        def flaky_worker():
            # 模拟在到达屏障前就抛异常退出的参与者
            raise RuntimeError("crashed before barrier")

        def waiting_worker():
            try:
                barrier.wait(timeout=0.2)
                with lock:
                    outcomes.append("passed")
            except (BarrierTimeoutError, BrokenBarrierError) as exc:
                with lock:
                    outcomes.append(type(exc).__name__)
            except Exception as exc:  # pragma: no cover
                collector.record(exc)

        threads = [threading.Thread(target=flaky_worker)]
        threads += [threading.Thread(target=waiting_worker) for _ in range(2)]
        run_threads(threads)
        collector.assert_empty(self)
        self.assertEqual(
            sorted(outcomes), ["BarrierTimeoutError", "BrokenBarrierError"]
        )
        self.assertTrue(barrier.broken)
        barrier.reset()
        barrier.check_invariants()


class TestBarrierStress(unittest.TestCase):
    """压力测试：复用屏障多轮，每轮恰好放行 parties 个线程，绝不重复放行。"""

    def test_reuse_no_double_release(self):
        parties, rounds = 8, 300
        barrier = FairBarrier(parties)
        collector = ErrorCollector()
        arrived = [0] * rounds
        passed = [0] * rounds
        lock = threading.Lock()

        def worker():
            try:
                for r in range(rounds):
                    with lock:
                        arrived[r] += 1
                    barrier.wait()
                    with lock:
                        # 关键不变量：我被放行时，本代必须已全部到齐
                        assert arrived[r] == parties, "未集齐就放行！"
                        passed[r] += 1
            except Exception as exc:
                collector.record(exc)

        threads = [threading.Thread(target=worker) for _ in range(parties)]
        run_threads(threads, join_timeout=60)
        collector.assert_empty(self)
        # 每轮恰好放行 parties 个线程：不多（重复放行）不少（丢失唤醒）
        self.assertEqual(passed, [parties] * rounds)
        # 每一轮 generation 恰好 +1，总数等于轮数 => 不存在重复放行
        self.assertEqual(barrier.generation, rounds)
        barrier.check_invariants()

    def test_arrival_indices_unique_per_generation(self):
        parties, rounds = 6, 100
        barrier = FairBarrier(parties)
        collector = ErrorCollector()
        indices_per_round = [[] for _ in range(rounds)]
        lock = threading.Lock()

        def worker():
            try:
                for r in range(rounds):
                    idx = barrier.wait()
                    with lock:
                        indices_per_round[r].append(idx)
            except Exception as exc:
                collector.record(exc)

        threads = [threading.Thread(target=worker) for _ in range(parties)]
        run_threads(threads, join_timeout=60)
        collector.assert_empty(self)
        for r in range(rounds):
            self.assertEqual(sorted(indices_per_round[r]), list(range(parties)))


if __name__ == "__main__":
    unittest.main()
