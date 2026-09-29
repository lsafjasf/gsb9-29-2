"""争用检测与缓解的边界用例自测（标准库 unittest）。"""
import unittest

from memory_model import MemoryModel, LINE_SIZE, LINE_TRANSFER_CYCLES
from counters import (PackedCounterArray, PaddedCounterArray,
                      SingleCounter, ShardedCounter)
from contention import analyze, ranking_signature
import workloads
from benchmark import measure, scenario_builders


def drive(n_threads, cores, op_factory, counter, iterations, write_every=1):
    fns = [(lambda t=t: op_factory(counter, t)) for t in range(n_threads)]
    workloads.run(fns, "deterministic")


class TestMemoryModel(unittest.TestCase):
    def test_line_accounting(self):
        mem = MemoryModel(2)
        addr = mem.alloc("x", 8)
        mem.write(0, addr, 0)                      # cold miss
        mem.write(0, addr, 0)                      # 本地命中
        mem.write(1, addr, 1)                      # 跨核转移
        st = mem.lines[0]
        self.assertEqual(st.writes, 3)
        self.assertEqual(st.cold_misses, 1)
        self.assertEqual(st.transfers, 1)
        self.assertEqual(st.cycles, 2 * LINE_TRANSFER_CYCLES + 1)
        self.assertEqual(st.writers, {0, 1})

    def test_alloc_alignment(self):
        mem = MemoryModel(1)
        mem.alloc("a", 8)
        base = mem.alloc("b", LINE_SIZE, align=LINE_SIZE)
        self.assertEqual(base % LINE_SIZE, 0)
        self.assertEqual(mem.symbols_for_line(base // LINE_SIZE), ["b"])

    def test_invalid_core_rejected(self):
        mem = MemoryModel(2)
        with self.assertRaises(ValueError):
            mem.write(2, 0, 0)


class TestContentionDetection(unittest.TestCase):
    def test_single_thread_no_contention(self):
        mem = MemoryModel(4)
        c = PackedCounterArray(mem, 4)
        drive(1, 4, lambda c_, t: workloads.array_own_slot_ops(c_, 4, t, 4, 1000), c, 1000)
        reports = analyze(mem)
        self.assertTrue(all(r.kind == "private" for r in reports))
        self.assertEqual(sum(r.transfers for r in reports), 0)

    def test_false_sharing_detected_on_packed(self):
        mem = MemoryModel(2)
        c = PackedCounterArray(mem, 2)   # 两个计数器挤在同一行
        drive(2, 2, lambda c_, t: workloads.array_own_slot_ops(c_, 2, t, 2, 100), c, 100)
        top = analyze(mem)[0]
        self.assertEqual(top.kind, "false-sharing")
        self.assertEqual(top.n_writers, 2)
        self.assertGreater(top.transfers, 0)
        self.assertEqual(len(top.symbols), 2)

    def test_padding_eliminates_false_sharing(self):
        mem = MemoryModel(2)
        c = PaddedCounterArray(mem, 2)
        drive(2, 2, lambda c_, t: workloads.array_own_slot_ops(c_, 2, t, 2, 100), c, 100)
        reports = analyze(mem)
        self.assertTrue(all(r.kind == "private" for r in reports))
        self.assertEqual(sum(r.transfers for r in reports), 0)

    def test_true_sharing_detected_on_single_counter(self):
        mem = MemoryModel(2)
        c = SingleCounter(mem)
        drive(2, 2, lambda c_, t: workloads.global_counter_ops(c_, t, 2, 100), c, 100)
        top = analyze(mem)[0]
        self.assertEqual(top.kind, "true-sharing")
        self.assertEqual(top.symbols, ["global"])
        self.assertGreater(top.transfers, 0)

    def test_sharded_counter_no_sharing_and_correct(self):
        mem = MemoryModel(4)
        n = 8
        c = ShardedCounter(mem, n)
        drive(n, 4, lambda c_, t: workloads.sharded_counter_ops(c_, t, 4, 500), c, 500)
        self.assertEqual(c.total(), n * 500)
        self.assertEqual(sum(r.transfers for r in analyze(mem)), 0)

    def test_sparse_writes_low_contention(self):
        mem = MemoryModel(4)
        c = PackedCounterArray(mem, 8)
        drive(8, 4, lambda c_, t: workloads.array_own_slot_ops(
            c_, 8, t, 4, 10000, write_every=1000), c, 10000, write_every=1000)
        reports = analyze(mem)
        total_writes = sum(r.writes for r in reports)
        total_transfers = sum(r.transfers for r in reports)
        self.assertLessEqual(total_writes, 8 * 10)   # 每线程仅约10次写
        self.assertLessEqual(total_transfers, total_writes)

    def test_oversubscription_threads_far_exceed_cores(self):
        mem = MemoryModel(2)
        n = 32
        c = PackedCounterArray(mem, n)
        drive(n, 2, lambda c_, t: workloads.array_own_slot_ops(c_, n, t, 2, 50), c, 50)
        reports = analyze(mem)
        self.assertTrue(any(r.kind == "false-sharing" for r in reports))
        self.assertEqual(c.total(), n * 50)

    def test_ranking_stable_across_runs(self):
        sigs = set()
        for _ in range(5):
            _, builder = scenario_builders(8, 4, 300, 1)[0][1][0]
            r = measure("紧凑数组", builder, 8, 4, 300, 1, mode="deterministic")
            sigs.add(ranking_signature(r.reports))
        self.assertEqual(len(sigs), 1)

    def test_counter_values_correct_under_contention(self):
        mem = MemoryModel(4)
        n, iters = 8, 300
        c = PackedCounterArray(mem, n)
        drive(n, 4, lambda c_, t: workloads.array_own_slot_ops(c_, n, t, 4, iters), c, iters)
        for i in range(n):
            self.assertEqual(c.value(i), iters)

    def test_real_mode_runs(self):
        mem = MemoryModel(2)
        c = PackedCounterArray(mem, 4)
        fns = [(lambda t=t: workloads.array_own_slot_ops(c, 4, t, 2, 200))
               for t in range(4)]
        workloads.run(fns, "real")
        self.assertEqual(c.total(), 4 * 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
