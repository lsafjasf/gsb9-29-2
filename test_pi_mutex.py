"""优先级继承库自测（unittest，仅标准库）。"""
import unittest

from pi_mutex import Task, Mutex, Scheduler


def make_classic(inheritance):
    """经典优先级反转场景：低持锁 / 中占 CPU / 高等锁。"""
    sch = Scheduler(inheritance=inheritance)
    M = Mutex("M")
    low = Task("low", 1, [("lock", M), ("work", 30), ("unlock", M)])
    med = Task("med", 5, [("work", 50)])
    high = Task("high", 10, [("lock", M), ("work", 5), ("unlock", M)])
    sch.add(low, start_at=0)
    sch.add(med, start_at=2)
    sch.add(high, start_at=4)
    return sch, low, med, high


def boost_events(sch, name):
    return [e for e in sch.events if e[1] == "boost" and e[2] == name]


def assert_all_restored(testcase, sch):
    for t in sch.tasks:
        testcase.assertEqual(t.boost, 0, f"{t.name} boost 未恢复")
        testcase.assertEqual(t.eff_prio, t.base_prio, f"{t.name} 有效优先级未恢复")
        testcase.assertEqual(t.held, [], f"{t.name} 仍持有锁")


class TestPriorityInheritance(unittest.TestCase):

    def test_single_waiter_boost_and_restore(self):
        """单等待者：高优先级阻塞时持有者被提升，释放后恢复原始优先级。"""
        sch, low, med, high = make_classic(inheritance=True)
        sch.run()
        events = boost_events(sch, "low")
        # t=4 高优先级阻塞 -> low 提升到 10（boost = 10 - 1 = 9）
        self.assertIn((4, "boost", "low", 0, 9), events)
        # low 释放后 boost 必须归零（恢复断言）
        self.assertIn(("boost", "low", 9, 0),
                      [(e[1], e[2], e[3], e[4]) for e in events])
        self.assertEqual(low.boost, 0)
        self.assertEqual(low.eff_prio, low.base_prio)
        # 量化：高优先级仅等待 low 剩余的 28 个滴答
        self.assertEqual(high.total_wait, 28)
        assert_all_restored(self, sch)

    def test_inversion_without_inheritance(self):
        """对照组：关闭继承时不发生提升，高优先级被中优先级无限拖后。"""
        sch, low, med, high = make_classic(inheritance=False)
        sch.run()
        self.assertEqual(boost_events(sch, "low"), [])
        # 高优先级等到 t=80（低优先级排在中优先级之后），等待 76 滴答
        self.assertEqual(high.total_wait, 76)
        assert_all_restored(self, sch)

    def test_no_contention(self):
        """无争用：加解锁不产生任何提升，优先级保持不变。"""
        sch = Scheduler()
        M = Mutex("M")
        solo = Task("solo", 3, [("lock", M), ("work", 5), ("unlock", M),
                                ("lock", M), ("work", 2), ("unlock", M)])
        sch.add(solo)
        sch.run()
        self.assertEqual(solo.lock_results, [True, True])
        self.assertEqual([e for e in sch.events if e[1] == "boost"], [])
        self.assertEqual(solo.boost, 0)
        self.assertEqual(solo.total_wait, 0)

    def test_multi_waiter_fifo_and_restore(self):
        """多等待者：提升到最高等待者；释放后按有效优先级交接，持有者恢复。"""
        sch = Scheduler()
        M = Mutex("M")
        low = Task("low", 1, [("lock", M), ("work", 10), ("unlock", M)])
        h1 = Task("h1", 10, [("lock", M), ("work", 3), ("unlock", M)])
        h2 = Task("h2", 8, [("lock", M), ("work", 3), ("unlock", M)])
        sch.add(low, start_at=0)
        sch.add(h2, start_at=1)   # h2 先阻塞（提升前），h1 随后再次推高
        sch.add(h1, start_at=2)
        sch.run()
        events = boost_events(sch, "low")
        # t=1 h2(8) 阻塞 -> 提升到 8；t=2 h1(10) 阻塞 -> 再提升到 10
        self.assertEqual(events, [(1, "boost", "low", 0, 7),
                                  (2, "boost", "low", 7, 9),
                                  (10, "boost", "low", 9, 0)])
        wakes = [e for e in sch.events if e[1] == "wake"]
        # 交接顺序：先 h1（有效优先级高），后 h2
        self.assertEqual([e[2] for e in wakes], ["h1", "h2"])
        self.assertEqual(h1.total_wait, 8)    # t=2..10
        self.assertEqual(h2.total_wait, 12)   # t=1..13
        assert_all_restored(self, sch)

    def test_nested_holding_chain_propagation(self):
        """嵌套持有：提升沿等待链传播（H->A->B），释放后逐级恢复。"""
        sch = Scheduler()
        M1, M2 = Mutex("M1"), Mutex("M2")
        b = Task("b", 1, [("lock", M2), ("work", 20), ("unlock", M2)])
        a = Task("a", 2, [("lock", M1), ("lock", M2), ("work", 5),
                          ("unlock", M2), ("unlock", M1)])
        h = Task("h", 10, [("lock", M1), ("work", 2), ("unlock", M1)])
        sch.add(b, start_at=0)
        sch.add(a, start_at=1)
        sch.add(h, start_at=3)
        sch.run()
        # t=1：a(2) 等 M2 -> b 提升到 2；t=3：h(10) 等 M1 -> a 提升到 10，
        # 并沿链传播使 b 进一步提升到 10
        self.assertIn((1, "boost", "b", 0, 1), boost_events(sch, "b"))
        self.assertIn((3, "boost", "a", 0, 8), boost_events(sch, "a"))
        self.assertIn((3, "boost", "b", 1, 9), boost_events(sch, "b"))
        # b 释放 M2 后立即恢复；a 释放 M1 后恢复
        self.assertIn((20, "boost", "b", 9, 0), boost_events(sch, "b"))
        self.assertIn((25, "boost", "a", 8, 0), boost_events(sch, "a"))
        self.assertEqual(h.total_wait, 22)    # t=3..25
        assert_all_restored(self, sch)

    def test_boost_kept_while_second_mutex_still_waited(self):
        """提升期间再次等待：释放一把锁后，另一把锁的等待者使提升只降不归零。"""
        sch = Scheduler()
        M1, M2 = Mutex("M1"), Mutex("M2")
        low = Task("low", 1, [("lock", M1), ("lock", M2), ("work", 6),
                              ("unlock", M1), ("work", 6), ("unlock", M2)])
        h1 = Task("h1", 10, [("lock", M1), ("work", 2), ("unlock", M1)])
        h2 = Task("h2", 8, [("lock", M2), ("work", 2), ("unlock", M2)])
        sch.add(low, start_at=0)
        sch.add(h2, start_at=1)   # h2 先在 M2 上阻塞
        sch.add(h1, start_at=2)   # h1 随后在 M1 上阻塞，再次推高
        sch.run()
        # t=1 h2(8) -> 提升到 8；t=2 h1(10) -> 提升到 10；
        # t=6 释放 M1 后降到 8（不是 0，M2 上仍有 h2）；t=14 释放 M2 后才归零
        self.assertEqual(boost_events(sch, "low"),
                         [(1, "boost", "low", 0, 7),
                          (2, "boost", "low", 7, 9),
                          (6, "boost", "low", 9, 7),
                          (14, "boost", "low", 7, 0)])
        self.assertEqual(h1.total_wait, 4)    # t=2..6
        self.assertEqual(h2.total_wait, 13)   # t=1..14
        assert_all_restored(self, sch)

    def test_timeout_release(self):
        """超时释放：等待超时返回 False，持有者提升随等待者退出而回收。"""
        sch = Scheduler()
        M = Mutex("M")
        low = Task("low", 1, [("lock", M), ("work", 30), ("unlock", M)])
        high = Task("high", 10, [("lock", M, 10), ("work", 2)])
        sch.add(low, start_at=0)
        sch.add(high, start_at=1)
        sch.run()
        # 超时：lock 结果为 False，等待恰为 10 个滴答
        self.assertEqual(high.lock_results, [False])
        self.assertEqual(high.total_wait, 10)
        self.assertIn((11, "timeout", "high", "M"), sch.events)
        # t=1 提升，t=11 超时后提升被回收
        self.assertEqual(boost_events(sch, "low"),
                         [(1, "boost", "low", 0, 9),
                          (11, "boost", "low", 9, 0)])
        assert_all_restored(self, sch)


if __name__ == "__main__":
    unittest.main(verbosity=2)
