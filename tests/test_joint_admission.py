"""Self-tests for the joint admission controller and the baseline.

Run:  python3 -m unittest discover -s tests -v   (from the repo root)
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from joint_admission import (AdmissionController, PerResourceController,
                             GRANTED, QUEUED, REJECTED)

CAP = {"cpu": 4, "memory": 8, "handles": 16}


class JointAdmissionTest(unittest.TestCase):
    def test_single_resource_tight(self):
        ctl = AdmissionController({"cpu": 2, "memory": 99, "handles": 99})
        s, _ = ctl.request("a", {"cpu": 2}, now=0.0)
        self.assertEqual(s, GRANTED)
        s, _ = ctl.request("b", {"cpu": 1}, now=1.0)
        self.assertEqual(s, QUEUED)  # only cpu is scarce
        granted = ctl.release("a", now=2.0)
        self.assertEqual(granted, ["b"])
        self.assertEqual(ctl.used["cpu"], 1)
        self.assertEqual(ctl.metrics.waits, [0.0, 1.0])

    def test_all_resources_tight(self):
        ctl = AdmissionController(CAP)
        s, _ = ctl.request("big", dict(CAP), now=0.0)
        self.assertEqual(s, GRANTED)
        s, _ = ctl.request("tiny", {"cpu": 1}, now=0.5)
        self.assertEqual(s, QUEUED)  # every resource is exhausted
        granted = ctl.release("big", now=3.0)
        self.assertEqual(granted, ["tiny"])

    def test_demand_exceeds_capacity_rejected(self):
        ctl = AdmissionController(CAP)
        s, _ = ctl.request("huge", {"cpu": 5}, now=0.0)
        self.assertEqual(s, REJECTED)
        s, _ = ctl.request("huge2", {"cpu": 4, "handles": 17}, now=0.0)
        self.assertEqual(s, REJECTED)
        self.assertEqual(ctl.metrics.rejected, 2)
        self.assertEqual(ctl.metrics.rejection_rate(), 1.0)
        # pool untouched: a fitting task is still admitted immediately
        s, _ = ctl.request("ok", {"cpu": 4}, now=0.0)
        self.assertEqual(s, GRANTED)

    def test_grant_is_atomic_no_partial_holding(self):
        ctl = AdmissionController(CAP)
        ctl.request("a", {"cpu": 4}, now=0.0)          # cpu exhausted
        s, _ = ctl.request("b", {"cpu": 1, "memory": 2}, now=1.0)
        self.assertEqual(s, QUEUED)
        # the queued task holds NOTHING (no hold-and-wait possible)
        self.assertNotIn("b", ctl.allocated)
        self.assertEqual(ctl.used["memory"], 0)

    def test_mid_task_partial_release_unblocks_queue(self):
        ctl = AdmissionController(CAP)
        ctl.request("a", dict(CAP), now=0.0)
        s, _ = ctl.request("b", {"cpu": 2, "memory": 4}, now=1.0)
        self.assertEqual(s, QUEUED)
        # 'a' releases half of its vector mid-task, keeps the rest
        granted = ctl.release("a", now=2.0,
                              resources={"cpu": 2, "memory": 4, "handles": 8})
        self.assertEqual(granted, ["b"])
        self.assertEqual(ctl.used, {"cpu": 4, "memory": 8, "handles": 8})
        # 'a' is still allocated (not completed) until final release
        self.assertIn("a", ctl.allocated)
        self.assertEqual(ctl.metrics.completed, 0)
        ctl.release("a", now=5.0)
        self.assertNotIn("a", ctl.allocated)
        self.assertEqual(ctl.metrics.completed, 1)

    def test_release_more_than_held_raises(self):
        ctl = AdmissionController(CAP)
        ctl.request("a", {"cpu": 2}, now=0.0)
        with self.assertRaises(ValueError):
            ctl.release("a", now=1.0, resources={"cpu": 3})
        with self.assertRaises(KeyError):
            ctl.release("ghost", now=1.0)

    def test_fifo_order_and_unknown_resource(self):
        ctl = AdmissionController(CAP)
        ctl.request("a", dict(CAP), now=0.0)
        ctl.request("b", {"cpu": 1}, now=1.0)
        ctl.request("c", {"cpu": 1}, now=2.0)
        granted = ctl.release("a", now=3.0)
        self.assertEqual(granted, ["b", "c"])  # FIFO
        with self.assertRaises(KeyError):
            ctl.request("x", {"gpu": 1}, now=4.0)

    def test_aging_prevents_starvation(self):
        # A big task queues behind a full pool; small tasks keep arriving.
        # Without aging the big task could be skipped forever.
        ctl = AdmissionController(CAP, aging_threshold=5.0)
        ctl.request("holder", dict(CAP), now=0.0)
        s, _ = ctl.request("big", {"cpu": 3, "memory": 6}, now=1.0)
        self.assertEqual(s, QUEUED)
        # holder keeps most resources; only 1 cpu / 2 mem free at t=2
        ctl.release("holder", now=2.0,
                    resources={"cpu": 1, "memory": 2, "handles": 16})
        # small tasks that fit arrive after 'big' is aged (t > 6)
        granted = ctl.request("small", {"cpu": 1, "memory": 2}, now=7.0)[1]
        self.assertEqual(granted, [])  # blocked behind aged 'big'
        ctl.release("holder", now=8.0)  # frees the rest
        self.assertIn("big", ctl.allocated)
        big_wait = ctl.metrics.waits[-1]
        self.assertLessEqual(big_wait, 8.0 - 1.0)

    def test_max_wait_timeout_rejects(self):
        ctl = AdmissionController(CAP, max_wait=10.0)
        ctl.request("holder", dict(CAP), now=0.0)
        s, _ = ctl.request("waiter", {"cpu": 1}, now=1.0)
        self.assertEqual(s, QUEUED)
        ctl.request("tick", {"cpu": 0}, now=20.0)  # any event applies timeouts
        self.assertEqual(ctl.metrics.rejected, 1)
        self.assertEqual(len(ctl.queue), 0)

    def test_utilization_metrics(self):
        ctl = AdmissionController({"cpu": 2, "memory": 2, "handles": 2})
        ctl.request("a", {"cpu": 1, "memory": 1, "handles": 1}, now=0.0)
        ctl.release("a", now=4.0)
        util = ctl.metrics.utilization(8.0)
        # 1 of 2 units used for 4 of 8 time units -> 25%
        for r in util:
            self.assertAlmostEqual(util[r], 0.25)


class DeadlockFreedomTest(unittest.TestCase):
    def test_baseline_deadlocks_where_joint_cannot(self):
        # Classic AB-BA: two queued tasks acquire cpu/memory in opposite
        # orders once a blocker frees the pool -> circular wait.
        cap = {"cpu": 1, "memory": 1, "handles": 1}
        base = PerResourceController(cap)
        base.request("blocker", {"cpu": 1, "memory": 1}, now=0.0)
        base.request("t1", {"cpu": 1, "memory": 1}, now=0.1,
                     order=["cpu", "memory"])
        base.request("t2", {"cpu": 1, "memory": 1}, now=0.2,
                     order=["memory", "cpu"])
        base.release("blocker", now=1.0)  # t1 grabs cpu, t2 grabs memory
        self.assertGreaterEqual(base.metrics.deadlocks, 1)

        joint = AdmissionController({"cpu": 1, "memory": 1, "handles": 1})
        s1, _ = joint.request("t1", {"cpu": 1, "memory": 1}, now=0.0)
        s2, _ = joint.request("t2", {"cpu": 1, "memory": 1}, now=0.1)
        self.assertEqual((s1, s2), (GRANTED, QUEUED))
        self.assertEqual(joint.metrics.deadlocks, 0)
        # t2 holds nothing while queued -> no circular wait can exist
        self.assertNotIn("t2", joint.allocated)

    def test_stress_never_overcommits_and_always_terminates(self):
        rng = random.Random(7)
        cap = {"cpu": 8, "memory": 16, "handles": 32}
        for _ in range(20):
            ctl = AdmissionController(cap, aging_threshold=5.0, max_wait=50.0)
            events = []
            for i in range(120):
                demand = {r: rng.randint(1, cap[r] // 2) for r in cap}
                events.append([rng.uniform(0, 100), "req", i, demand])
                events.append([None, "rel", i, None])
            reqs = {e[2]: e for e in events if e[1] == "req"}
            for e in events:
                if e[1] == "rel":
                    start = reqs[e[2]][0]
                    e[0] = start + rng.uniform(0.1, 5.0)
            events.sort(key=lambda e: e[0])
            for t, kind, i, demand in events:
                if kind == "req":
                    ctl.request(i, demand, now=t)
                elif i in ctl.allocated:
                    ctl.release(i, now=t)
                # invariant: never over-committed, queued tasks hold nothing
                for r in cap:
                    self.assertLessEqual(ctl.used[r], cap[r])
                for qid, _, _ in ctl.queue:
                    self.assertNotIn(qid, ctl.allocated)
            # drain the pool: release everything still running
            for tid in list(ctl.allocated):
                ctl.release(tid, now=200.0)
            # every queued task fits now (pool empty) -> queue must empty
            self.assertEqual(len(ctl.queue), 0)
            self.assertEqual(len(ctl.allocated), 0)
            self.assertEqual(ctl.metrics.deadlocks, 0)

    def test_baseline_stress_deadlocks_but_joint_does_not(self):
        import compare
        tasks = compare.gen_workload(200, seed=3, load=0.6)
        joint = AdmissionController(compare.CAPACITY, aging_threshold=5.0,
                                    max_wait=60.0)
        m_joint, _ = compare.simulate(tasks, joint, seed=3)
        base = PerResourceController(compare.CAPACITY)
        m_base, _ = compare.simulate(tasks, base, baseline=True, seed=3)
        self.assertEqual(m_joint.deadlocks, 0)
        self.assertGreater(m_base.deadlocks, 0)
        self.assertGreater(m_joint.completed, m_base.completed)


if __name__ == "__main__":
    unittest.main()
