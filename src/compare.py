"""Discrete-event simulation comparing joint vector admission against
independent per-resource admission under an identical workload.

Run:  python3 src/compare.py [--tasks N] [--seed S] [--load F]
"""

import argparse
import heapq
import random
import sys

from joint_admission import AdmissionController, PerResourceController

CAPACITY = {"cpu": 8, "memory": 16, "handles": 64}


def gen_workload(n_tasks, seed, load):
    """Each task: (arrival, demand, duration, mid_release_fraction)."""
    rng = random.Random(seed)
    tasks = []
    t = 0.0
    for i in range(n_tasks):
        t += rng.expovariate(load)
        demand = {
            "cpu": rng.choice([1, 1, 2, 2, 3, 4]),
            "memory": rng.choice([1, 2, 2, 4, 6, 8]),
            "handles": rng.choice([4, 8, 8, 16, 24, 32]),
        }
        duration = rng.uniform(2.0, 8.0)
        mid = 0.5 if rng.random() < 0.3 else 0.0  # 30% release half mid-task
        tasks.append((t, demand, duration, mid))
    return tasks


def simulate(tasks, controller, baseline=False, seed=0):
    rng = random.Random(seed + 1)
    events = []  # (time, seq, kind, task_id)
    seq = 0

    def push(time, kind, task_id):
        nonlocal seq
        heapq.heappush(events, (time, seq, kind, task_id))
        seq += 1

    info = {}  # task_id -> (demand, duration, mid, granted_at)
    for i, (arrival, demand, duration, mid) in enumerate(tasks):
        info[i] = [demand, duration, mid, None]
        push(arrival, "arrive", i)

    def on_granted(task_ids, now):
        for tid in task_ids:
            demand, duration, mid, granted_at = info[tid]
            if granted_at is not None:
                continue  # already scheduled
            info[tid][3] = now
            if mid:
                push(now + duration / 2.0, "mid", tid)
            push(now + duration, "done", tid)

    last_t = 0.0
    while events:
        now, _, kind, tid = heapq.heappop(events)
        last_t = now
        demand, duration, mid, granted_at = info[tid]
        if kind == "arrive":
            if baseline:
                order = list(demand)
                rng.shuffle(order)  # task-specific acquisition order
                _, granted = controller.request(tid, demand, now=now,
                                                order=order)
            else:
                _, granted = controller.request(tid, demand, now=now)
            on_granted(granted, now)
        elif kind == "mid":
            half = {r: v // 2 for r, v in demand.items() if v // 2 > 0}
            if half:
                granted = controller.release(tid, now=now, resources=half)
                on_granted(granted, now)
        else:  # done
            granted = controller.release(tid, now=now)
            on_granted(granted, now)
    controller.metrics.tick(last_t)
    return controller.metrics, last_t


def report(name, metrics, horizon):
    util = metrics.utilization(horizon)
    wait = metrics.wait_stats()
    print("  %-28s" % name)
    print("    completed / granted / rejected : %d / %d / %d"
          % (metrics.completed, metrics.granted, metrics.rejected))
    print("    rejection rate                 : %.2f%%"
          % (100.0 * metrics.rejection_rate()))
    print("    deadlocks detected             : %d" % metrics.deadlocks)
    print("    utilization cpu/mem/handles    : %.1f%% / %.1f%% / %.1f%%"
          % (100 * util["cpu"], 100 * util["memory"], 100 * util["handles"]))
    print("    queue wait avg / p95 / max (s) : %.2f / %.2f / %.2f"
          % (wait["avg"], wait["p95"], wait["max"]))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", type=int, default=400)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--load", type=float, default=0.9,
                    help="arrival rate (tasks/time-unit)")
    args = ap.parse_args(argv)

    tasks = gen_workload(args.tasks, args.seed, args.load)
    print("Workload: %d tasks, capacity %s, seed=%d, arrival rate=%.2f"
          % (len(tasks), CAPACITY, args.seed, args.load))
    print()

    joint = AdmissionController(CAPACITY, aging_threshold=5.0, max_wait=60.0)
    m_joint, t_joint = simulate(tasks, joint, baseline=False, seed=args.seed)
    report("Joint vector admission", m_joint, t_joint)
    print()

    base = PerResourceController(CAPACITY)
    m_base, t_base = simulate(tasks, base, baseline=True, seed=args.seed)
    report("Per-resource admission (baseline)", m_base, t_base)
    return 0


if __name__ == "__main__":
    sys.exit(main())
