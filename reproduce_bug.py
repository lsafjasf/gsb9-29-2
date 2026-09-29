"""Reproduce the production bugs against barrier_buggy, and verify the
same scenarios pass against the fixed barrier.

Usage:
    python3 reproduce_bug.py --impl buggy   # shows the failures
    python3 reproduce_bug.py --impl fixed   # same scenarios, all clean
"""

import argparse
import threading
import time

import barrier as fixed_barrier
import barrier_buggy


def make_barrier(impl, parties):
    if impl == "buggy":
        return barrier_buggy.BuggyBarrier(parties)
    return fixed_barrier.Barrier(parties)


def scenario_lost_wakeup(impl, parties=8, timeout=1.0):
    """All `parties` threads arrive; every one must be released.
    Buggy: notify() wakes only one, the rest time out."""
    bar = make_barrier(impl, parties)
    arrived = [0]
    released = [0]
    timed_out = [0]
    lock = threading.Lock()

    def worker():
        with lock:
            arrived[0] += 1
        try:
            bar.wait(timeout=timeout)
            with lock:
                released[0] += 1
        except (TimeoutError, fixed_barrier.BarrierTimeoutError):
            with lock:
                timed_out[0] += 1
        except fixed_barrier.BrokenBarrierError:
            with lock:
                timed_out[0] += 1

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(parties)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout + 2)

    ok = released[0] == parties and timed_out[0] == 0
    print("  arrived=%d released=%d stuck/timed-out=%d -> %s"
          % (arrived[0], released[0], timed_out[0],
             "OK" if ok else "BUG REPRODUCED: lost wakeup"))
    return ok


def scenario_state_residue(impl, parties=4):
    """Round 1: some threads time out and raise. Round 2 must still
    require ALL parties to arrive before releasing anyone.
    Buggy: leaked count from round 1 releases round 2 early."""
    bar = make_barrier(impl, parties)

    def late_worker():
        try:
            bar.wait(timeout=0.2)
        except Exception:
            pass

    round1 = [threading.Thread(target=late_worker, daemon=True)
              for _ in range(parties - 1)]
    for t in round1:
        t.start()
    for t in round1:
        t.join()

    if impl == "fixed":
        bar.reset()  # operator recovers the barrier after the failure

    arrivals = [0]
    released_when_arrivals = []
    lock = threading.Lock()

    def worker():
        with lock:
            arrivals[0] += 1
        try:
            bar.wait(timeout=2.0)
            with lock:
                released_when_arrivals.append(arrivals[0])
        except Exception:
            pass

    round2 = [threading.Thread(target=worker, daemon=True) for _ in range(parties)]
    for t in round2:
        t.start()
    for t in round2:
        t.join(3.0)

    early = [a for a in released_when_arrivals if a < parties]
    ok = not early and len(released_when_arrivals) == parties
    detail = ("released with only %s/%d arrivals" % (early, parties)) if early \
        else "released=%d/%d all at full arrival" % (len(released_when_arrivals), parties)
    print("  %s -> %s" % (detail, "OK" if ok else "BUG REPRODUCED: state residue"))
    return ok


def scenario_concurrent_stress(impl, parties=8, rounds=200):
    """High-frequency reuse with concurrent arrivals. Every thread must
    complete every round; no thread may lap another by >1 round."""
    bar = make_barrier(impl, parties)
    progress = [0] * parties
    errors = []

    def worker(idx):
        try:
            for _ in range(rounds):
                bar.wait(timeout=5.0)
                progress[idx] += 1
                if progress[idx] - min(progress) > 1:
                    errors.append("thread %d lapped the barrier" % idx)
                    return
        except Exception as exc:
            errors.append("thread %d round %d: %r" % (idx, progress[idx], exc))

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(parties)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(30.0)
    elapsed = time.monotonic() - start

    done = min(progress)
    ok = not errors and done == rounds
    print("  rounds completed=%d/%d errors=%d elapsed=%.2fs -> %s"
          % (done, rounds, len(errors), elapsed,
             "OK" if ok else "BUG REPRODUCED: " + (errors[0] if errors else "hang")))
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--impl", choices=["buggy", "fixed"], required=True)
    args = parser.parse_args()

    print("implementation: %s" % args.impl)
    results = []
    print("[1] lost wakeup (all parties arrive, all must be released)")
    results.append(scenario_lost_wakeup(args.impl))
    print("[2] state residue (timeout in round 1 must not corrupt round 2)")
    results.append(scenario_state_residue(args.impl))
    print("[3] concurrent high-frequency reuse (8 threads x 200 rounds)")
    results.append(scenario_concurrent_stress(args.impl))

    if all(results):
        print("RESULT: all scenarios clean")
        return 0
    print("RESULT: %d/%d scenarios reproduced bugs" % (results.count(False), len(results)))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
