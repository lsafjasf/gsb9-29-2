"""Regression tests for the coroutine scheduler.

Run:  python3 test_scheduler.py
or:   python3 -m unittest test_scheduler -v
"""

import unittest

from scheduler import Scheduler
from buggy_scheduler import StrictPriorityScheduler

NUM_PRIORITIES = 8
HIGH = NUM_PRIORITIES - 1
LOW = 0
AGING = 4
LONG_STEPS = 2000
N_LOW = 50
LOW_STEPS = 3


def work(steps):
    """A coroutine that performs ``steps`` units of work, one per yield."""
    for _ in range(steps):
        yield


# A generator that yields N times is resumed N+1 times: the final resume
# detects completion and runs zero steps.  A task of S steps therefore
# occupies S+1 dispatch turns.
DISPATCHES_PER_TASK = LONG_STEPS + 1


def spawn_load(sched, n_long=1):
    """n_long long, always-ready high-priority tasks + many low-priority ones."""
    longs = [sched.spawn(work(LONG_STEPS), priority=HIGH, name="long-%d" % i)
             for i in range(n_long)]
    lows = [sched.spawn(work(LOW_STEPS), priority=LOW, name="low-%d" % i)
            for i in range(N_LOW)]
    return longs, lows


class StarvationReproTest(unittest.TestCase):
    """Reproduce the original bug: strict priority starves low-priority tasks."""

    def test_buggy_scheduler_starves_low_priority(self):
        sched = StrictPriorityScheduler(num_priorities=NUM_PRIORITIES)
        _, lows = spawn_load(sched)
        sched.run()
        first = min(sched.task(t).first_dispatch for t in lows)
        # Not a single low-priority coroutine runs before the long task drains.
        self.assertEqual(first, DISPATCHES_PER_TASK)

    def test_buggy_wait_grows_with_load(self):
        # More high-priority load -> proportionally longer starvation window.
        # The wait is unbounded and ends only when the load drops.
        waits = []
        for n_long in (1, 2, 3):
            sched = StrictPriorityScheduler(num_priorities=NUM_PRIORITIES)
            _, lows = spawn_load(sched, n_long=n_long)
            sched.run()
            waits.append(min(sched.task(t).first_dispatch for t in lows))
        self.assertEqual(waits, [DISPATCHES_PER_TASK,
                                 2 * DISPATCHES_PER_TASK,
                                 3 * DISPATCHES_PER_TASK])

    def test_fixed_scheduler_removes_starvation(self):
        sched = Scheduler(num_priorities=NUM_PRIORITIES, aging_interval=AGING)
        longs, lows = spawn_load(sched)
        sched.run()
        bound = sched.wait_bound(LOW)
        first = [sched.task(t).first_dispatch for t in lows]
        for t in lows:
            self.assertLessEqual(sched.task(t).first_dispatch, bound)
        # Low-priority coroutines start long before the long task drains ...
        self.assertLess(min(first), LONG_STEPS)
        # ... yet every coroutine still completes.
        self.assertTrue(all(sched.task(t).done for t in longs + lows))


class WaitBoundTest(unittest.TestCase):
    def test_every_wait_streak_respects_bound(self):
        # Includes requeue streaks and a non-unit time slice.
        sched = Scheduler(num_priorities=NUM_PRIORITIES, aging_interval=AGING,
                          time_slice=2)
        tids = [sched.spawn(work(steps), priority=prio)
                for prio, steps in [(HIGH, 500), (LOW, 2), (LOW, 2), (3, 40)]]
        sched.run()
        for tid in tids:
            task = sched.task(tid)
            bound = sched.wait_bound(task.priority)
            self.assertLessEqual(task.max_wait, bound)
            self.assertLessEqual(task.max_wait_steps,
                                 sched.time_slice * bound)


class DeterminismTest(unittest.TestCase):
    SCENARIO = [(7, 30), (0, 5), (3, 12), (7, 8), (0, 20), (3, 1), (5, 9)]

    def _run_once(self):
        sched = Scheduler(num_priorities=NUM_PRIORITIES, aging_interval=AGING)
        for i, (prio, steps) in enumerate(self.SCENARIO):
            sched.spawn(work(steps), priority=prio, name="t%d" % i)
        return sched.run()

    def test_execution_sequence_is_reproducible(self):
        self.assertEqual(self._run_once(), self._run_once())

    def test_ties_broken_by_fifo(self):
        # Equal priorities -> strict FIFO round-robin by enqueue order
        # (2 steps + 1 completion turn = 3 turns per task).
        sched = Scheduler(num_priorities=NUM_PRIORITIES, aging_interval=2)
        tids = [sched.spawn(work(2), priority=4) for _ in range(3)]
        self.assertEqual(sched.run(), tids * 3)


class EdgeCaseTest(unittest.TestCase):
    def test_single_coroutine(self):
        sched = Scheduler(num_priorities=NUM_PRIORITIES, aging_interval=AGING)
        tid = sched.spawn(work(5), priority=3, name="only")
        self.assertEqual(sched.run(), [tid] * 6)
        self.assertEqual(sched.task(tid).steps, 5)
        self.assertTrue(sched.task(tid).done)
        self.assertEqual(sched.task(tid).first_dispatch, 0)

    def test_all_same_priority_round_robin(self):
        sched = Scheduler(num_priorities=NUM_PRIORITIES, aging_interval=1)
        tids = [sched.spawn(work(3), priority=2) for _ in range(4)]
        self.assertEqual(sched.run(), tids * 4)

    def test_priority_inversion_via_aging(self):
        # A low-priority coroutine ready first must run within its bound even
        # though higher-priority coroutines keep it below strict static order.
        sched = Scheduler(num_priorities=NUM_PRIORITIES, aging_interval=AGING)
        low = sched.spawn(work(1), priority=LOW, name="low")
        [sched.spawn(work(50), priority=HIGH, name="high-%d" % i)
         for i in range(4)]
        sched.run()
        low_task = sched.task(low)
        self.assertLessEqual(low_task.first_dispatch, sched.wait_bound(LOW))
        # Aging inverts the static order: the low coroutine runs long before
        # the 200 steps of high-priority work could drain.
        self.assertLess(low_task.first_dispatch, 4 * 50)
        self.assertTrue(low_task.done)

    def test_empty_ready_queue(self):
        sched = Scheduler(num_priorities=NUM_PRIORITIES, aging_interval=AGING)
        self.assertIsNone(sched.step())
        self.assertEqual(sched.run(), [])
        self.assertEqual(len(sched), 0)
        # The queue empties again once every coroutine is done.
        tid = sched.spawn(work(2), priority=1)
        sched.run()
        self.assertTrue(sched.task(tid).done)
        self.assertIsNone(sched.step())
        self.assertEqual(len(sched), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
