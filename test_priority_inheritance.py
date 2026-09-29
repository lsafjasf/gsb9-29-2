"""优先级继承库的自测（仅标准库 unittest）。

运行：python3 -m unittest test_priority_inheritance -v
"""

import unittest

from priority_inheritance import Mutex, Scheduler, Thread, lock, sleep, unlock, work


def make_classic(pi_enabled, medium_work=2000, low_work=8):
    """经典反转场景：L(1) 持锁工作，M(2) 长时间占 CPU，H(3) 等同一把锁。"""
    m = Mutex("M")

    def low():
        yield lock(m)
        yield work(low_work)
        yield unlock(m)

    def med():
        yield work(medium_work)

    def high():
        yield lock(m)
        yield work(2)
        yield unlock(m)

    sched = Scheduler(pi_enabled)
    low_t = sched.add_thread(Thread("L", 1, low()))
    med_t = sched.add_thread(Thread("M", 2, med(), start_at=1))
    high_t = sched.add_thread(Thread("H", 3, high(), start_at=2))
    return sched, m, low_t, med_t, high_t


def boost_restore_events(sched, name):
    return [e for e in sched.events
            if e[1] in ("boost", "restore") and e[2] == name]


class PriorityInheritanceTest(unittest.TestCase):

    def assert_restored(self, sched):
        """恢复断言：仿真结束后所有线程必须回到基础优先级，锁全部干净。"""
        for t in sched.threads:
            self.assertEqual(t.eff_prio, t.base_prio,
                             f"{t.name} 未恢复原始优先级")
            self.assertFalse(t.held, f"{t.name} 仍持有锁")
        for m in sched.mutexes:
            self.assertIsNone(m.owner, f"{m.name} 仍有持有者")
            self.assertFalse(m.waiters, f"{m.name} 仍有等待者")

    # ---------------------------------------------------------- 单等待者

    def test_single_waiter_boost_and_restore(self):
        sched, m, low_t, _, high_t = make_classic(pi_enabled=True)
        sched.run()
        # 提升：H 在 tick 2 阻塞后，L 被提升到 H 的优先级 3
        self.assertIn((2, "boost", "L", 1, 3), boost_restore_events(sched, "L"))
        # 恢复：释放后 L 必须降回基础优先级 1
        self.assertIn((11, "restore", "L", 3, 1), boost_restore_events(sched, "L"))
        # 高优先级等待时长：tick 2 阻塞 -> tick 11 获锁 = 9 tick
        self.assertEqual(high_t.last_wait, 9)
        self.assert_restored(sched)

    def test_inversion_without_pi(self):
        """对照组：关闭 PI 时，中优先级线程把高优先级拖延到自身结束。"""
        sched, m, low_t, _, high_t = make_classic(pi_enabled=False)
        sched.run()
        self.assertEqual(boost_restore_events(sched, "L"), [])  # 无任何提升
        self.assertEqual(high_t.last_wait, 2008)                # 远大于开启时的 9
        self.assert_restored(sched)

    # ---------------------------------------------------------- 无争用

    def test_no_contention(self):
        m = Mutex("M")

        def high():
            yield lock(m)
            yield work(3)
            yield unlock(m)

        sched = Scheduler(pi_enabled=True)
        h = sched.add_thread(Thread("H", 5, high()))
        sched.run()
        self.assertEqual(h.total_wait, 0)                       # 立即获锁
        self.assertEqual(boost_restore_events(sched, "H"), [])  # 无提升/恢复事件
        self.assertEqual(h.eff_prio, 5)
        self.assert_restored(sched)

    # ------------------------------------------- 多等待者 + 嵌套持有 + 提升期间再次等待

    def test_nested_holding_and_rewait_during_boost(self):
        """L 同时持有 M1、M2；H1(5) 等 M1 使 L 提升后，H2(6) 又来等 M2。

        恢复判定顺序：释放 M2 时 L 仍被 M1 的等待者提升（6->5），
        再释放 M1 才彻底复原（5->1）。整条链 1->5->6->5->1。
        """
        m1, m2 = Mutex("M1"), Mutex("M2")

        def low():
            yield lock(m1)
            yield lock(m2)
            yield work(6)
            yield unlock(m2)     # 先放 M2：仍被 M1 的等待者提升
            yield work(2)
            yield unlock(m1)     # 再放 M1：恢复原始优先级

        def h1():
            yield lock(m1)
            yield work(1)
            yield unlock(m1)

        def h2():                # 在 L 已被提升期间再次等待另一把锁
            yield lock(m2)
            yield work(1)
            yield unlock(m2)

        sched = Scheduler(pi_enabled=True)
        sched.add_thread(Thread("L", 1, low()))
        sched.add_thread(Thread("H1", 5, h1(), start_at=2))
        sched.add_thread(Thread("H2", 6, h2(), start_at=3))
        sched.run()
        self.assertEqual(boost_restore_events(sched, "L"), [
            (2, "boost", "L", 1, 5),     # H1 等 M1
            (3, "boost", "L", 5, 6),     # 提升期间 H2 又等 M2，取 max
            (10, "restore", "L", 6, 5),  # 释放 M2：仍被 M1 的等待者提升
            (15, "restore", "L", 5, 1),  # 释放 M1：彻底恢复
        ])
        self.assert_restored(sched)

    def test_multiple_waiters_same_mutex_highest_first(self):
        """同一把锁的多个等待者：提升到 max，释放时最高优先级等待者优先获锁。"""
        m = Mutex("M")

        def low():
            yield lock(m)
            yield work(3)
            yield unlock(m)

        def mid():
            yield lock(m)
            yield unlock(m)

        def high():
            yield lock(m)
            yield unlock(m)

        sched = Scheduler(pi_enabled=True)
        sched.add_thread(Thread("L", 1, low()))
        sched.add_thread(Thread("M1", 2, mid(), start_at=1))
        sched.add_thread(Thread("H", 3, high(), start_at=2))
        sched.run()
        self.assertEqual(boost_restore_events(sched, "L"),
                         [(1, "boost", "L", 1, 2), (2, "boost", "L", 2, 3),
                          (6, "restore", "L", 3, 1)])
        grants = [e[3] for e in sched.events if e[1] == "grant"]
        self.assertEqual(grants, ["L", "H", "M1"])   # H 虽晚到，但优先获锁
        self.assert_restored(sched)

    # ---------------------------------------------------------- 等待链传递

    def test_transitive_boost_chain(self):
        """H 等 L1 的锁，L1 等 L2 的锁 => L2 也被提升到 H 的优先级。"""
        m1, m2 = Mutex("M1"), Mutex("M2")

        def l1():
            yield lock(m1)
            yield sleep(2)       # 让出 CPU，保证 L2 先持有 M2，形成等待链
            yield lock(m2)
            yield work(2)
            yield unlock(m2)
            yield unlock(m1)

        def l2():
            yield lock(m2)
            yield work(4)
            yield unlock(m2)

        def high():
            yield lock(m1)
            yield work(1)
            yield unlock(m1)

        sched = Scheduler(pi_enabled=True)
        sched.add_thread(Thread("L1", 1, l1()))
        sched.add_thread(Thread("L2", 1, l2()))
        sched.add_thread(Thread("H", 3, high(), start_at=4))
        sched.run()
        # H 在 tick 4 阻塞于 M1：L1 被提升，并沿等待链传递给 L2
        self.assertIn((4, "boost", "L1", 1, 3), boost_restore_events(sched, "L1"))
        self.assertIn((4, "boost", "L2", 1, 3), boost_restore_events(sched, "L2"))
        self.assertIn((9, "restore", "L2", 3, 1), boost_restore_events(sched, "L2"))
        self.assertIn((13, "restore", "L1", 3, 1), boost_restore_events(sched, "L1"))
        self.assert_restored(sched)

    # ---------------------------------------------------------- 超时释放

    def test_timeout_releases_boost(self):
        """等待者超时放弃后，持有者必须立即被取消提升（恢复原始优先级）。"""
        m = Mutex("M")
        result = {}

        def low():
            yield lock(m)
            yield work(10)       # 即使被提升也来不及在 5 tick 内放锁
            yield unlock(m)

        def med():
            yield work(100)

        def high():
            ok = yield lock(m, timeout=5)
            result["acquired"] = ok
            if ok:
                yield unlock(m)

        sched = Scheduler(pi_enabled=True)
        low_t = sched.add_thread(Thread("L", 1, low()))
        sched.add_thread(Thread("M", 2, med(), start_at=1))
        sched.add_thread(Thread("H", 3, high(), start_at=2))
        sched.run()
        self.assertFalse(result["acquired"])                   # 超时获锁失败
        self.assertIn((2, "boost", "L", 1, 3), boost_restore_events(sched, "L"))
        # 超时发生的 tick 7（2+5）即恢复，而不是等 low 最终放锁时
        self.assertIn((7, "restore", "L", 3, 1), boost_restore_events(sched, "L"))
        self.assertTrue(low_t.done)                            # 超时后 low 照常完成
        self.assert_restored(sched)

    def test_acquire_before_timeout(self):
        """超时时间充足时，PI 让持有者优先跑完，等待者在超时前获锁。"""
        m = Mutex("M")
        result = {}

        def low():
            yield lock(m)
            yield work(10)
            yield unlock(m)

        def med():
            yield work(100)

        def high():
            ok = yield lock(m, timeout=20)
            result["acquired"] = ok
            if ok:
                yield unlock(m)

        sched = Scheduler(pi_enabled=True)
        sched.add_thread(Thread("L", 1, low()))
        sched.add_thread(Thread("M", 2, med(), start_at=1))
        h = sched.add_thread(Thread("H", 3, high(), start_at=2))
        sched.run()
        self.assertTrue(result["acquired"])
        self.assertEqual(h.last_wait, 11)          # tick 2 阻塞 -> tick 13 获锁
        self.assert_restored(sched)


if __name__ == "__main__":
    unittest.main(verbosity=2)
