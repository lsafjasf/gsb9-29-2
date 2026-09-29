"""Stable starvation reproduction + before/after comparison.

Run:  python3 -m sched_demo.reproduce

Scenario A (the reported bug):
    * one HIGH-priority hog that yields Work(B) once and never yields again;
    * many LOW-priority coroutines that are ready at the same time.
    On the naive strict-priority scheduler the lows do not get a single tick
    until the whole burst B is over: their first-start time grows without
    bound with B ("they only run when load drops").
    On the fixed scheduler every low is served in the FIRST round; the
    worst intra-round wait is (n-1)*q, independent of B.

Scenario B (priority inversion):
    * a LOW task holds a lock and yields; medium-priority hogs crowd in;
    * a HIGH task blocks on that lock.
    Naive delay scales with total medium workload (unbounded inversion);
    fixed delay is bounded by (n-1)*quantum plus the holder's slice.
"""

from __future__ import annotations

from typing import Iterator

from . import scheduler as fixed
from . import naive as buggy


def hog(burst: int) -> Iterator[object]:
    yield fixed.Work(burst)


def worker(burst: int) -> Iterator[object]:
    yield fixed.Work(burst)


def scenario_starvation(
    n_low: int = 50, quantum: int = 20, low_burst: int = 30, bursts=(1000, 5000, 20000)
) -> None:
    print("=" * 72)
    print("Scenario A: one non-yielding high-priority hog + %d low tasks" % n_low)
    print("=" * 72)
    print(
        "%8s | %26s | %26s | bound(n=%d,q=%d)"
        % ("burst B", "naive: first low tick", "fixed: max low wait", n_low, quantum)
    )
    print("-" * 72)

    for burst in bursts:
        # ---- naive (buggy) ----
        nb = buggy.NaiveScheduler()
        for i in range(n_low):
            nb.spawn("L%d" % i, priority=1, gen=worker(low_burst))
        nb.spawn("H", priority=0, gen=hog(burst))
        nb.run()
        naive_first = min(t.first_start for n, t in nb.tasks.items() if n != "H")

        # ---- fixed ----
        fx = fixed.Scheduler(quantum=quantum, inherit=True)
        for i in range(n_low):
            fx.spawn("L%d" % i, priority=1, gen=worker(low_burst))
        fx.spawn("H", priority=0, gen=hog(burst))
        fx.run()
        fixed_max_wait = max(
            dispatch - ready
            for name, t in fx.tasks.items()
            if name != "H"
            for ready, _rstart, dispatch in t.waits
        )
        bound = n_low * quantum  # (n-1)*q, n = n_low + 1
        print(
            "%8d | %26d | %26d | %d"
            % (burst, naive_first, fixed_max_wait, bound)
        )

        # Assertions that make the starvation "reproduced" / "fixed".
        assert naive_first == burst, (
            "expected the naive scheduler to starve lows until t=%d, got %d"
            % (burst, naive_first)
        )
        assert fixed_max_wait <= bound, (
            "fixed scheduler exceeded (n-1)*q: %d > %d" % (fixed_max_wait, bound)
        )

    print("=> naive first-low-start == B (unbounded as B grows)")
    print("=> fixed max low wait == %d == (n-1)*q, independent of B" % bound)


# --------------------------------------------------------------------------- #
# Scenario B: priority inversion
# --------------------------------------------------------------------------- #
#
# A "start gate" lock held initially by D forces the deterministic arrival
# order on both schedulers: H blocks on the gate; L grabs the resource lock;
# D opens the gate; only then does H try the resource lock - and blocks on L.


def low_holder(res, cs_len):
    yield res.acquire()
    yield fixed.Work(1)
    yield fixed.Yield()
    yield fixed.Work(cs_len)
    yield res.release()


def medium_hog(burst):
    yield fixed.Yield()  # let L grab the resource lock first
    yield fixed.Work(burst)


def high_waiter(gate, res):
    yield gate.acquire()  # released by D only after L holds res
    yield res.acquire()
    yield fixed.Work(10)
    yield res.release()


def gate_opener(gate):
    yield gate.release()


def build_inversion(sched, n_medium, medium_burst, cs_len):
    gate = sched.new_lock("gate")
    res = sched.new_lock("res")
    sched.spawn("H", priority=0, gen=high_waiter(gate, res))
    for i in range(n_medium):
        sched.spawn("M%d" % i, priority=1, gen=medium_hog(medium_burst))
    sched.spawn("L", priority=2, gen=low_holder(res, cs_len))
    opener = sched.spawn("D", priority=3, gen=gate_opener(gate))
    gate.holder = opener  # gate starts locked; D releases it once L is armed


def scenario_inversion(n_medium=5, quantum=10, medium_burst=1000, cs_len=8):
    print()
    print("=" * 72)
    print(
        "Scenario B: low lock holder + %d medium hogs (burst=%d each) + high waiter"
        % (n_medium, medium_burst)
    )
    print("=" * 72)

    nb = buggy.NaiveScheduler()
    build_inversion(nb, n_medium, medium_burst, cs_len)
    nb.run()
    h_block, h_wake = block_window(nb.trace, "H")
    naive_delay = h_wake - h_block

    fx = fixed.Scheduler(quantum=quantum, inherit=True)
    build_inversion(fx, n_medium, medium_burst, cs_len)
    fx.run()
    h_block, h_wake = block_window(fx.trace, "H")
    fixed_delay = h_wake - h_block
    bound = (n_medium + 1) * quantum + cs_len

    print("naive : H blocked for %5d ticks  (scales with medium total work)" % naive_delay)
    print(
        "fixed : H blocked for %5d ticks  (bound ~ %d, independent of medium burst)"
        % (fixed_delay, bound)
    )
    assert naive_delay >= n_medium * medium_burst, naive_delay
    assert fixed_delay <= bound, (fixed_delay, bound)
    print("=> inversion on the fixed scheduler is bounded and burst-independent")


def block_window(trace, name):
    """Return (clock when `name` blocked, clock when it was woken)."""
    clock = 0
    block_t = None
    for ev in trace:
        kind = ev[0]
        if kind == "run":
            clock = ev[-1]  # naive: (run,name,start,end); fixed: (run,rd,name,s,e)
        elif kind == "block" and ev[1] == name:
            block_t = clock
        elif kind == "wake" and ev[1] == name:
            return block_t, clock
    raise AssertionError("no block/wake pair for %r" % name)


def main():
    scenario_starvation()
    scenario_inversion()
    print()
    print("ALL REPRODUCTION CHECKS PASSED")


if __name__ == "__main__":
    main()
