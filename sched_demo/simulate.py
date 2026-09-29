"""Simulation: verify the waiting-time upper bound of the fixed scheduler.

Run:  python3 -m sched_demo.simulate

Bound (derived in README_zh.md):

    For every round r let n_r be the number of ready tasks snapshotted at
    the start of the round. Each snapshot task is dispatched exactly once
    in that round with a fixed budget of q = quantum ticks, so the CPU-time
    a task waits inside the round before its first dispatch is at most

        W_r <= (n_r - 1) * q.

    A task that becomes ready *during* round r (lock wake-up / newly ready)
    is dispatched from round r+1 on; the extra wall-time before round r+1
    starts is at most n_r * q, hence the universal arrival-to-dispatch bound

        W <= (2*n_max - 1) * q,   n_max = max over rounds of n_r.

This script checks BOTH bounds on a grid of configurations and on the
exact starvation workload from the bug report. It prints a table (also
saved to simulation_results.txt) showing that the observed maxima never
exceed the bounds and that the per-round bound is tight.
"""

from __future__ import annotations

import os
from typing import Iterator

from . import scheduler as sched
from .scheduler import Work


# --------------------------------------------------------------------------- #
# Measurement helpers
# --------------------------------------------------------------------------- #


def collect_waits(s: sched.Scheduler):
    """Measure, for every dispatch, the CPU service received before it.

    The theoretical bound (n-1)*q is stated in units of CPU service
    provided earlier in the same round: a snapshot round runs n tasks each
    capped at q ticks, so the k-th dispatched task receives at most
    (k-1)*q <= (n-1)*q ticks of CPU service from peers first. This script
    reconstructs that quantity directly from the deterministic trace and
    checks it for every dispatch in every round.

    It additionally checks the wall-clock delay of tasks woken mid-round
    against the universal bound (2*n_max-1)*q.
    """
    # dispatch order and CPU service per task per round
    order = {}        # rno -> [task names]
    service = {}      # (rno, name) -> CPU ticks used in that round
    for ev in s.trace:
        if ev[0] == "dispatch":
            _, rno, name, _at = ev
            order.setdefault(rno, []).append(name)
        elif ev[0] == "run":
            _, rno, name, a, b = ev
            service[(rno, name)] = service.get((rno, name), 0) + (b - a)

    samples = []  # (category, service_wait, bound, rno, name)
    worst_service = 0
    n_max = 0
    for rno, names in sorted(order.items()):
        n = len(names)
        n_max = max(n_max, n)
        accumulated = 0
        for name in names:
            bound = (n - 1) * s.quantum
            samples.append(("snapshot", accumulated, bound, rno, name))
            worst_service = max(worst_service, accumulated)
            accumulated += service.get((rno, name), 0)
            assert accumulated <= n * s.quantum, "round exceeded n*q"

    # Wall-clock check for tasks becoming ready mid-round (wake-ups):
    # ready_at lives in the arrival round; dispatch happens next round.
    round_starts = sorted((ev[2], ev[1]) for ev in s.trace if ev[0] == "round")

    def round_at(clock):
        cur = None
        for rstart, rno in round_starts:  # sorted by start time
            if rstart <= clock:
                cur = rno
            else:
                break
        return cur

    n_by_round = {rno: len(v) for rno, v in order.items()}
    universal = (2 * n_max - 1) * s.quantum if n_max else 0
    mid_ok = True
    worst_wall = 0
    for t in s.tasks.values():
        for ready_at, dispatch_rstart, dispatch_at in t.waits:
            r_arrive = round_at(ready_at)
            r_dispatch = round_at(dispatch_rstart)
            if r_arrive is None or r_dispatch is None or r_arrive == r_dispatch:
                continue
            # arrival round must immediately precede the dispatch round
            assert r_dispatch == r_arrive + 1, (r_arrive, r_dispatch)
            bound = n_by_round[r_arrive] * s.quantum + (
                n_by_round[r_dispatch] - 1
            ) * s.quantum
            delay = dispatch_at - ready_at
            mid_ok &= delay <= bound <= universal
            worst_wall = max(worst_wall, delay)

    service_ok = all(d <= b for _, d, b, _, _ in samples)
    return samples, n_max, universal, service_ok and mid_ok, worst_service, worst_wall


# --------------------------------------------------------------------------- #
# Workload generators
# --------------------------------------------------------------------------- #


def starvation_workload(n_low: int, low_burst: int, hog_burst: int):
    """The exact bug-report shape: one non-yielding hog + many lows."""

    def build(q: int) -> sched.Scheduler:
        s = sched.Scheduler(quantum=q)
        for i in range(n_low):
            s.spawn("L%03d" % i, priority=1, gen=_worker(low_burst))
        s.spawn("H", priority=0, gen=_hog(hog_burst))
        return s

    return build


def mixed_workload(n_tasks: int, max_burst: int):
    """Mixed priorities, mixed burst lengths; all ready at t=0."""

    def build(q: int) -> sched.Scheduler:
        s = sched.Scheduler(quantum=q)
        # Deterministic pseudo-bursts (no random module needed, but random
        # with a fixed seed is equally deterministic; use a LCG for clarity).
        x = 1234567
        for i in range(n_tasks):
            x = (1103515245 * x + 12345) & 0x7FFFFFFF
            burst = 1 + x % max_burst
            s.spawn(
                "T%03d" % i,
                priority=i % 3,  # priorities 0,1,2 cycling
                gen=_worker(burst),
            )
        return s

    return build


def wakeup_workload(n_peers: int, peer_burst: int):
    """Mid-round wake-up stress workload.

    Each waiter has its own lock, all held initially by one low-priority
    holder. The holder occupies a long critical section and releases every
    lock *inside one round* (back-to-back zero-cost events); all waiters
    become ready mid-round and are first dispatched in the following round,
    exercising the universal (2*n_max-1)*q arrival-to-dispatch bound.
    Filler tasks keep both the release round and the dispatch round busy.
    """

    def build(q: int) -> sched.Scheduler:
        sch = sched.Scheduler(quantum=q)
        locks = [sch.new_lock("lk%02d" % i) for i in range(n_peers)]
        holder = sch.spawn(
            "R",
            priority=2,
            gen=_multi_holder(locks, hold_ticks=(n_peers + 2) * q + 3),
        )
        for lk in locks:
            lk.holder = holder
        for i, lk in enumerate(locks):
            sch.spawn("W%02d" % i, priority=i % 2, gen=_waiter(lk, peer_burst))
            long_burst = (n_peers + 3) * q
            sch.spawn("F%02d" % i, priority=1, gen=_worker(long_burst))
        return sch

    return build


def _multi_holder(locks, hold_ticks: int) -> Iterator[object]:
    yield Work(hold_ticks)  # preempted round after round; release is 3 ticks
    for lk in locks:        # into the final round (holder runs first there)
        yield lk.release()


def _hog(burst: int) -> Iterator[object]:
    yield Work(burst)


def _worker(burst: int) -> Iterator[object]:
    remaining = burst
    while remaining > 0:
        step = min(remaining, 1 + (remaining % 7))
        yield Work(step)
        remaining -= step


def _waiter(lk, burst: int) -> Iterator[object]:
    yield lk.acquire()
    yield Work(burst)
    # Does not release: one-shot hand-off by the holder (and the waiter
    # priority differs, exercising priority ordering on wake).


def _one_shot_holder(lk, hold_ticks: int) -> Iterator[object]:
    yield Work(hold_ticks)   # preempted round after round, still holding lk
    yield lk.release()


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def run_case(title, builder, q):
    sch = builder(q)
    sch.run()
    samples, n_max, universal, ok, worst_service, worst_wall = collect_waits(sch)
    tight_bound = (n_max - 1) * q if n_max else 0
    tight_hit = n_max > 1 and worst_service == tight_bound
    return {
        "title": title,
        "q": q,
        "rounds": sum(1 for ev in sch.trace if ev[0] == "round"),
        "n_max": n_max,
        "worst_service": worst_service,
        "worst_wall": worst_wall,
        "tight_bound": tight_bound,
        "universal": universal,
        "ok": ok,
        "tight_hit": tight_hit,
    }


def main():
    results = []

    for n_low in (5, 20, 50):
        for q in (5, 10, 20):
            for hog_burst in (q, 10 * q, 100 * q, 1000 * q):
                results.append(
                    run_case(
                        "starvation n=%d B=%d" % (n_low + 1, hog_burst),
                        starvation_workload(n_low, q + 3, hog_burst),
                        q,
                    )
                )

    for n in (4, 10, 25):
        for q in (4, 8, 16):
            results.append(run_case("mixed n=%d" % n, mixed_workload(n, 5 * q), q))

    for n_peers in (3, 8, 15):
        for q in (5, 10):
            results.append(
                run_case(
                    "wakeups n=%d" % (n_peers + 1),
                    wakeup_workload(n_peers, 2 * q + 1),
                    q,
                )
            )

    lines = []
    header = (
        "%-24s %3s %6s %4s %10s %9s %9s %9s %4s"
        % ("case", "q", "rounds", "nmax", "svcWaitMax", "wallMax", "(n-1)q", "(2n-1)q", "OK")
    )
    lines.append(header)
    lines.append("-" * len(header))
    for r in results:
        lines.append(
            "%-24s %3d %6d %4d %10d %9d %9d %9d %4s"
            % (
                r["title"], r["q"], r["rounds"], r["n_max"],
                r["worst_service"], r["worst_wall"],
                r["tight_bound"], r["universal"],
                "yes" if r["ok"] else "NO",
            )
        )

    all_ok = all(r["ok"] for r in results)
    tight_hits = [r for r in results if r["tight_hit"]]
    lines.append("")
    lines.append("cases: %d; all per-dispatch bounds satisfied: %s" % (len(results), all_ok))
    lines.append("cases attaining the tight bound (n_max-1)*q: %d / %d" % (len(tight_hits), len(results)))
    lines.append("")
    lines.append("svcWaitMax: max CPU ticks delivered by peers earlier in the same round")
    lines.append("wallMax   : max ready->dispatch wall delay among mid-round wake-ups")

    out = "\n".join(lines)
    print(out)
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "simulation_results.txt"), "w", encoding="utf-8") as f:
        f.write(out + "\n")

    assert all_ok
    assert tight_hits


if __name__ == "__main__":
    main()
