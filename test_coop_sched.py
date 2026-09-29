"""coop_sched 自测：公平性、唤醒不丢（时序）、统计、边界用例。

运行：python3 -m unittest test_coop_sched -v
"""

import time
import unittest

from coop_sched import Scheduler, Sleep


def make_gen(body):
    """把无 yield 的函数包装成生成器（用于立即结束的协程）。"""
    def wrapper(*args):
        body(*args)
        if False:
            yield
    return wrapper


class EdgeCaseTests(unittest.TestCase):
    def test_empty_scheduler(self):
        """无协程：run 立即返回，统计全零。"""
        sched = Scheduler()
        stats = sched.run()
        self.assertEqual(stats.spawned, 0)
        self.assertEqual(stats.finished, 0)
        self.assertEqual(stats.steps, 0)
        self.assertEqual(stats.rounds, [])
        self.assertLess(stats.wall_time, 0.5)

    def test_single_coroutine(self):
        """单协程：让步若干次后正常结束。"""
        log = []

        def worker():
            for i in range(3):
                log.append(i)
                yield
            return "done"

        sched = Scheduler()
        task = sched.spawn(worker, name="solo")
        stats = sched.run()
        self.assertEqual(log, [0, 1, 2])
        self.assertTrue(task.done)
        self.assertEqual(task.result, "done")
        self.assertEqual(stats.finished, 1)
        self.assertEqual(stats.steps, 4)  # 3 次让步 + 1 次返回
        self.assertEqual(len(stats.rounds), 4)

    def test_wake_finished_task(self):
        """唤醒已结束的协程：安全 no-op，不影响调度器。"""
        sched = Scheduler()

        def quick_gen():
            return 1
            yield

        task = sched.spawn(quick_gen, name="gone")
        stats = sched.run()
        self.assertTrue(task.done)
        # 结束后唤醒：返回 False，计入 wake_after_done，不崩溃
        self.assertFalse(sched.wake(task))
        self.assertEqual(stats.wake_after_done, 1)
        # 再跑一轮（队列已空）也不受影响
        stats2 = sched.run()
        self.assertEqual(stats2.rounds, [])

    def test_wake_finished_task_during_run(self):
        """运行期间，协程 A 唤醒已结束的协程 B。"""
        events = []

        def finisher():
            events.append("B done")
            return
            yield

        def waker(task_b):
            yield  # 让 B 先跑完
            events.append(f"wake result={sched.wake(task_b)}")
            yield

        sched = Scheduler()
        task_b = sched.spawn(finisher, name="B")
        sched.spawn(waker, task_b, name="A")
        stats = sched.run()
        self.assertIn("wake result=False", events)
        self.assertEqual(stats.wake_after_done, 1)
        self.assertEqual(stats.finished, 2)

    def test_spawn_rejects_non_generator(self):
        sched = Scheduler()
        with self.assertRaises(TypeError):
            sched.spawn(lambda: 42)

    def test_negative_sleep_rejected(self):
        with self.assertRaises(ValueError):
            Sleep(-1)


class FairnessTests(unittest.TestCase):
    def test_round_robin_interleaving(self):
        """公平性：所有协程的第 k 步都先于任何协程的第 k+1 步。"""
        log = []
        N, STEPS = 8, 5

        def worker(tid):
            for s in range(STEPS):
                log.append((tid, s))
                yield

        sched = Scheduler()
        for i in range(N):
            sched.spawn(worker, i, name=f"w{i}")
        sched.run()

        steps_seen = [s for _, s in log]
        # 全局步数序列必须非递减 => 严格的逐轮轮转，无人被插队饿死
        self.assertEqual(steps_seen, sorted(steps_seen))
        self.assertEqual(len(log), N * STEPS)

    def test_many_coroutines(self):
        """海量协程：20000 个协程全部完成，且保持公平。"""
        N, STEPS = 20_000, 3
        counter = [0] * N

        def worker(tid):
            for _ in range(STEPS):
                counter[tid] += 1
                yield

        sched = Scheduler()
        for i in range(N):
            sched.spawn(worker, i)
        start = time.monotonic()
        stats = sched.run()
        elapsed = time.monotonic() - start

        self.assertEqual(counter, [STEPS] * N)
        self.assertEqual(stats.finished, N)
        self.assertEqual(stats.steps, N * (STEPS + 1))
        # 每轮执行量 = 当前存活协程数（逐轮快照调度）
        self.assertEqual(stats.rounds[0].tasks_run, N)
        self.assertEqual(stats.rounds[-1].tasks_run, N)
        self.assertLess(elapsed, 10.0, "海量协程调度过慢")

    def test_starvation_detection(self):
        """防饥饿：长时间不让步的协程被检测并报告。"""
        reported = []
        sched = Scheduler(
            starvation_threshold=0.05,
            on_starvation=reported.append,
        )

        def hog():
            deadline = time.monotonic() + 0.20  # 200ms 不让步
            while time.monotonic() < deadline:
                pass
            yield

        def polite():
            for _ in range(3):
                yield

        sched.spawn(hog, name="hog")
        sched.spawn(polite, name="polite")
        stats = sched.run()

        self.assertTrue(reported, "未检测到饥饿事件")
        self.assertEqual(reported[0].task_name, "hog")
        self.assertGreaterEqual(reported[0].duration, 0.19)
        self.assertEqual(len(stats.starvation_events), 1)
        # 正常协程不应被误报
        self.assertNotIn("polite", [e.task_name for e in reported])


class WakeupTests(unittest.TestCase):
    def test_wake_before_sleep_not_lost(self):
        """时序1：唤醒发生在睡眠之前 —— sleep 被立即消费，协程不睡死。"""
        order = []

        def waker(target):
            order.append("waker: wake B (B still ready)")
            sched.wake(target)   # B 尚未睡眠，登记 wake_pending
            yield
            order.append("waker done")

        def sleeper():
            order.append("B: about to sleep 10s")
            yield Sleep(10.0)    # 有 pending 唤醒 => 立即返回，不真睡
            order.append("B: resumed")
            return "B-result"

        sched = Scheduler()
        task_b = sched.spawn(sleeper, name="B")
        sched.spawn(waker, task_b, name="A")
        start = time.monotonic()
        stats = sched.run()
        elapsed = time.monotonic() - start

        self.assertEqual(task_b.result, "B-result")
        self.assertLess(elapsed, 1.0, "唤醒丢失：协程真的睡了 10s")
        self.assertEqual(stats.wakeups_delivered, 1)
        self.assertEqual(order[-1], "waker done")

    def test_wake_during_sleep_not_lost(self):
        """时序2：唤醒发生在睡眠之中 —— 协程被提前唤醒而非睡满全程。"""
        timings = {}

        def sleeper():
            t0 = time.monotonic()
            yield Sleep(5.0)     # 打算睡 5s
            timings["slept"] = time.monotonic() - t0
            return "awake"

        def waker(target):
            yield Sleep(0.05)    # 50ms 后唤醒对方
            sched.wake(target)

        sched = Scheduler()
        task_a = sched.spawn(sleeper, name="sleeper")
        sched.spawn(waker, task_a, name="waker")
        stats = sched.run()

        self.assertEqual(task_a.result, "awake")
        self.assertLess(timings["slept"], 1.0, "睡眠中的唤醒丢失")
        self.assertGreaterEqual(timings["slept"], 0.04)
        self.assertEqual(stats.wakeups_delivered, 1)

    def test_mass_wake_before_sleep(self):
        """压力时序：N 个协程睡眠前全部被唤醒，无一丢失、无一真睡。"""
        N = 1000
        resumed = []

        def sleepers(tid):
            yield Sleep(30.0)
            resumed.append(tid)

        def mass_waker(targets):
            for t in targets:
                sched.wake(t)    # 全部在对方睡眠前唤醒
            yield

        sched = Scheduler()
        tasks = [sched.spawn(sleepers, i) for i in range(N)]
        sched.spawn(mass_waker, tasks, name="waker")
        start = time.monotonic()
        stats = sched.run()
        elapsed = time.monotonic() - start

        self.assertEqual(sorted(resumed), list(range(N)))
        self.assertEqual(stats.wakeups_delivered, N)
        self.assertLess(elapsed, 5.0, "存在唤醒丢失，有协程睡满 30s")

    def test_sleep_deadline_ordering(self):
        """无外部唤醒时，睡眠按截止时间先后到期。"""
        order = []

        def sleeper(tag, delay):
            yield Sleep(delay)
            order.append(tag)

        sched = Scheduler()
        sched.spawn(sleeper, "slow", 0.09)
        sched.spawn(sleeper, "fast", 0.03)
        sched.spawn(sleeper, "mid", 0.06)
        sched.run()
        self.assertEqual(order, ["fast", "mid", "slow"])


class StatsTests(unittest.TestCase):
    def test_stats_consistency(self):
        """统计输出：每轮执行量之和 == 总步数；summary 可打印。"""
        def worker():
            for _ in range(4):
                yield

        sched = Scheduler()
        for _ in range(3):
            sched.spawn(worker)
        stats = sched.run()

        self.assertEqual(sum(r.tasks_run for r in stats.rounds), stats.steps)
        self.assertEqual(stats.spawned, stats.finished)
        text = stats.summary()
        self.assertIn("scheduler stats", text)
        self.assertIn("round", text)
        self.assertIn("wakeups delivered", text)


if __name__ == "__main__":
    unittest.main()
