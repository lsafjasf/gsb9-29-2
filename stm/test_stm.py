"""STM 自测: 回滚断言 / 嵌套 / 显式放弃 / 并发 / 热点 / 重试上限。"""
import threading
import time
import unittest

import stm


class SingleTransactionTest(unittest.TestCase):
    def setUp(self):
        stm.reset_stats()

    def test_single_transaction_commit(self):
        a, b = stm.TVar(10), stm.TVar(20)

        def body(tx):
            tx.write(a, tx.read(a) - 5)
            tx.write(b, tx.read(b) + 5)
            return "ok"

        self.assertEqual(stm.atomically(body), "ok")
        self.assertEqual(a.peek(), 5)
        self.assertEqual(b.peek(), 25)
        s = stm.get_stats()
        self.assertEqual(s["commits"], 1)
        self.assertEqual(s["conflicts"], 0)

    def test_read_your_writes(self):
        a = stm.TVar(1)
        seen = []

        def body(tx):
            tx.write(a, 42)
            seen.append(tx.read(a))

        stm.atomically(body)
        self.assertEqual(seen, [42])
        self.assertEqual(a.peek(), 42)


class RollbackAssertionTest(unittest.TestCase):
    """回滚断言: 无论是显式放弃还是冲突, 写集必须整体不生效。"""

    def setUp(self):
        stm.reset_stats()

    def test_abort_rolls_back_all_writes(self):
        a, b = stm.TVar(100), stm.TVar(200)

        def body(tx):
            tx.write(a, 1)   # 先写两个变量
            tx.write(b, 2)
            tx.abort()       # 再显式放弃

        with self.assertRaises(stm.AbortTransaction):
            stm.atomically(body)
        # 断言: 两个变量都保持原值, 不存在部分写入
        self.assertEqual(a.peek(), 100)
        self.assertEqual(b.peek(), 200)
        self.assertEqual(stm.get_stats()["aborts"], 1)

    def test_conflict_rollback_no_partial_write(self):
        a, b = stm.TVar(100), stm.TVar(200)
        barrier = threading.Barrier(2)
        conflict_seen = []

        def body(tx):
            tx.read(a)
            if not conflict_seen:
                barrier.wait(timeout=5)
            tx.write(a, 1)
            tx.write(b, 2)

        def interferer():
            barrier.wait(timeout=5)
            stm.atomically(lambda tx: tx.write(a, 999))

        t = threading.Thread(target=interferer)
        t.start()
        original_conflicts = stm.ConflictError
        try:
            stm.atomically(body, max_retries=0)
        except stm.RetryLimitExceeded:
            conflict_seen.append(True)
        t.join()
        # 冲突回滚后: a 是干扰线程的值, b 绝不能被部分写入
        self.assertTrue(conflict_seen)
        self.assertEqual(a.peek(), 999)
        self.assertEqual(b.peek(), 200)


class NestedTransactionTest(unittest.TestCase):
    def setUp(self):
        stm.reset_stats()

    def test_nested_commit_merges_into_outer(self):
        x, y = stm.TVar(0), stm.TVar(0)

        def body(tx):
            tx.write(x, 1)
            with stm.transaction(tx) as inner:
                inner.write(y, 2)
            # 嵌套提交后外层可读到自己刚合并的值
            self.assertEqual(tx.read(y), 2)

        stm.atomically(body)
        self.assertEqual((x.peek(), y.peek()), (1, 2))

    def test_nested_abort_preserves_outer_writes(self):
        x, y = stm.TVar(0), stm.TVar(0)

        def body(tx):
            tx.write(x, 1)                    # 外层已做的改动
            with stm.transaction(tx) as inner:
                inner.write(y, 2)
                inner.abort()                 # 嵌套显式放弃
            # 嵌套回滚不影响外层: x 的写入仍在
            self.assertEqual(tx.read(x), 1)

        stm.atomically(body)
        self.assertEqual(x.peek(), 1)         # 外层改动保留
        self.assertEqual(y.peek(), 0)         # 嵌套改动被丢弃

    def test_outer_abort_discards_nested_commits(self):
        x, y = stm.TVar(0), stm.TVar(0)

        def body(tx):
            with stm.transaction(tx) as inner:
                inner.write(y, 2)             # 嵌套正常提交 (合并进外层)
            tx.write(x, 1)
            tx.abort()                        # 外层放弃 -> 全部回滚

        with self.assertRaises(stm.AbortTransaction):
            stm.atomically(body)
        self.assertEqual((x.peek(), y.peek()), (0, 0))

    def test_deeply_nested_abort_only_rolls_back_innermost(self):
        a, b, c = stm.TVar(0), stm.TVar(0), stm.TVar(0)

        def body(tx):
            tx.write(a, 1)
            with stm.transaction(tx) as mid:
                mid.write(b, 2)
                with stm.transaction(mid) as inner:
                    inner.write(c, 3)
                    inner.abort()             # 只回滚最内层

        stm.atomically(body)
        self.assertEqual((a.peek(), b.peek(), c.peek()), (1, 2, 0))


class ConcurrencyTest(unittest.TestCase):
    THREADS = 8
    OPS = 200

    def setUp(self):
        stm.reset_stats()

    def test_no_conflict_concurrency(self):
        # 每个线程只动自己的变量: 提交应零冲突
        vars_ = [stm.TVar(0) for _ in range(self.THREADS)]

        def worker(idx):
            for _ in range(self.OPS):
                stm.atomically(lambda tx: tx.write(vars_[idx],
                                                   tx.read(vars_[idx]) + 1))

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(self.THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([v.peek() for v in vars_], [self.OPS] * self.THREADS)
        s = stm.get_stats()
        self.assertEqual(s["commits"], self.THREADS * self.OPS)
        self.assertEqual(s["conflicts"], 0)

    def test_high_contention_hotspot_no_lost_update(self):
        # 所有线程对同一热点计数器 read-modify-write: 冲突必然发生,
        # 但最终值必须等于总增量 (无丢失更新)。
        hotspot = stm.TVar(0)
        total = self.THREADS * self.OPS

        def bump(tx):
            v = tx.read(hotspot)
            time.sleep(0.0001)  # 模拟事务内业务耗时, 拉开读-提交窗口
            tx.write(hotspot, v + 1)

        def worker():
            for _ in range(self.OPS):
                stm.atomically(bump, max_retries=10_000)

        threads = [threading.Thread(target=worker)
                   for _ in range(self.THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(hotspot.peek(), total)
        s = stm.get_stats()
        self.assertGreater(s["conflicts"], 0)   # 热点场景确实产生了冲突
        self.assertEqual(s["commits"], total)   # 且全部最终提交成功


class RetryLimitTest(unittest.TestCase):
    def setUp(self):
        stm.reset_stats()

    def test_retry_limit_exhausted(self):
        a = stm.TVar(0)
        stm._force_next_conflicts(10)  # 测试钩子: 强制接下来 10 次提交冲突

        with self.assertRaises(stm.RetryLimitExceeded):
            stm.atomically(lambda tx: tx.write(a, 1), max_retries=3)

        s = stm.get_stats()
        # 首次尝试 + 3 次重试 = 4 次冲突, 重试 3 次后放弃
        self.assertEqual(s["conflicts"], 4)
        self.assertEqual(s["retries"], 3)
        self.assertEqual(s["commits"], 0)
        self.assertEqual(a.peek(), 0)  # 从未提交, 值不变

    def test_retry_eventually_succeeds(self):
        a = stm.TVar(0)
        stm._force_next_conflicts(2)  # 前 2 次冲突, 第 3 次成功

        stm.atomically(lambda tx: tx.write(a, 7), max_retries=5)
        self.assertEqual(a.peek(), 7)
        s = stm.get_stats()
        self.assertEqual(s["conflicts"], 2)
        self.assertEqual(s["commits"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
