"""Simulation validating the wait-time upper bound of the fixed scheduler.

Bound (derived in BOUND.md): a ready coroutine with base priority p waits at
most

    W(p)       = aging_interval * (num_priorities - p) + (N - 1)   dispatches
    W_steps(p) = time_slice * W(p)                                 steps

where N is the maximum ready-queue length during its wait.

Run:  python3 simulate.py
Exits non-zero if any observed wait exceeds the bound.
"""

import random
import sys

from scheduler import Scheduler
from buggy_scheduler import StrictPriorityScheduler


def work(steps):
    for _ in range(steps):
        yield


def check(label, sched, workloads):
    """Run ``workloads`` [(priority, steps), ...] and verify every wait
    streak (first dispatch and every requeue) against the bound."""
    tids = [sched.spawn(work(steps), priority=prio, name="t%d" % i)
            for i, (prio, steps) in enumerate(workloads)]
    sched.run()
    n = sched.max_ready
    ok = True
    worst_ratio, worst_wait, worst_bound = 0.0, 0, 1
    for tid in tids:
        task = sched.task(tid)
        bound = sched.wait_bound(task.priority, n)
        if task.max_wait > bound:
            ok = False
        if task.max_wait_steps > sched.time_slice * bound:
            ok = False
        if task.max_wait * 1.0 / bound > worst_ratio:
            worst_ratio = task.max_wait / bound
            worst_wait, worst_bound = task.max_wait, bound
    print("%-38s N=%-4d P=%d A=%d q=%d  max_wait=%-5d bound(lowest)=%-5d "
          "max_ratio=%.3f  %s"
          % (label, n, sched.num_priorities, sched.aging_interval,
             sched.time_slice, worst_wait, worst_bound, worst_ratio,
             "OK" if ok else "BOUND VIOLATED"))
    return ok


def scenario_repro():
    print("== Scenario 1: repro load (1 long high-priority task, 50 low-priority) ==")
    buggy = StrictPriorityScheduler(num_priorities=8)
    buggy.spawn(work(2000), priority=7, name="long")
    lows = [buggy.spawn(work(3), priority=0, name="low-%d" % i) for i in range(50)]
    buggy.run()
    waits = [buggy.task(t).first_dispatch for t in lows]
    print("buggy (strict priority): first low-priority dispatch at t=%d, "
          "last at t=%d  -> starved until the long task drained"
          % (min(waits), max(waits)))

    fixed = Scheduler(num_priorities=8, aging_interval=4)
    fixed.spawn(work(2000), priority=7, name="long")
    lows = [fixed.spawn(work(3), priority=0, name="low-%d" % i) for i in range(50)]
    fixed.run()
    waits = [fixed.task(t).first_dispatch for t in lows]
    bound = fixed.wait_bound(0)
    print("fixed (aging):           first low-priority dispatch at t=%d, "
          "last at t=%d  (bound W(0) = %d)" % (min(waits), max(waits), bound))
    print()


def scenario_adversarial():
    print("== Scenario 2: adversarial always-ready mix (every band occupied) ==")
    ok = True
    for n in (1, 2, 5, 10, 50, 200):
        sched = Scheduler(num_priorities=8, aging_interval=4)
        workloads = [(i % 8, 30) for i in range(n)]
        ok &= check("all bands, always ready", sched, workloads)
    # Extreme spread: one lowest-priority task vs many top-priority ones.
    sched = Scheduler(num_priorities=8, aging_interval=4)
    ok &= check("1 lowest vs 49 top", sched, [(0, 10)] + [(7, 10)] * 49)
    print()
    return ok


def scenario_fuzz(trials=500, seed=20261001):
    print("== Scenario 3: seeded random fuzz (%d trials) ==" % trials)
    rng = random.Random(seed)
    ok = True
    for i in range(trials):
        sched = Scheduler(num_priorities=rng.randint(1, 8),
                          aging_interval=rng.randint(1, 6),
                          time_slice=rng.randint(1, 4))
        workloads = [(rng.randrange(sched.num_priorities),
                      rng.randint(1, 40))
                     for _ in range(rng.randint(1, 30))]
        ok &= check("fuzz #%d" % i, sched, workloads) if i < 5 else ok
        if i >= 5:
            # Quiet mode for the remaining trials, but still verify.
            tids = [sched.spawn(work(steps), priority=prio)
                    for prio, steps in workloads]
            sched.run()
            n = sched.max_ready
            for tid in tids:
                task = sched.task(tid)
                bound = sched.wait_bound(task.priority, n)
                if task.max_wait > bound or \
                        task.max_wait_steps > sched.time_slice * bound:
                    ok = False
                    print("fuzz #%d: BOUND VIOLATED" % i)
    print("(trials #5..%d verified quietly)" % (trials - 1))
    print()
    return ok


def main():
    scenario_repro()
    ok = scenario_adversarial()
    ok &= scenario_fuzz()
    if not ok:
        print("RESULT: bound violated")
        return 1
    print("RESULT: all observed waits within the bound W(p) = "
          "A*(P-p) + (N-1) dispatches, W_steps = q * W(p)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
