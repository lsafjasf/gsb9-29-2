"""Demo workload: fair workers, sleepers, an event, and one hog.

Run: python3 demo.py
"""

import time

from scheduler import Scheduler, sleep, wait


def main():
    sched = Scheduler(max_run_time=0.02)
    ev = sched.event()

    def worker(tag, n):
        for _ in range(n):
            yield

    def sleeper(tag, delay):
        yield sleep(delay)

    def waiter():
        yield wait(ev)

    def waker():
        yield
        ev.set()

    def hog():
        start = time.perf_counter()
        while time.perf_counter() - start < 0.08:
            pass  # refuses to yield: should be flagged
        yield

    for i in range(5):
        sched.create_task(worker(i, 4), name=f"worker-{i}")
    sched.create_task(sleeper("nap-short", 0.01), name="nap-short")
    sched.create_task(sleeper("nap-long", 0.05), name="nap-long")
    sched.create_task(waiter(), name="waiter")
    sched.create_task(waker(), name="waker")
    sched.create_task(hog(), name="hog")

    started = time.perf_counter()
    stats = sched.run()
    wall = time.perf_counter() - started

    print(f"wall time         : {wall:.3f}s")
    print(f"tasks finished    : {stats['tasks_finished']}/{stats['tasks_total']}")
    print(f"rounds            : {stats['rounds']}")
    print(f"tasks per round   : {stats['round_task_counts']}")
    print(f"context switches  : {stats['context_switches']}")
    print(f"deadlocked        : {stats['deadlocked'] or 'none'}")
    if stats["violations"]:
        print("starvation violations:")
        for v in stats["violations"]:
            print(f"  round {v['round']:>2}: task {v['task']!r} "
                  f"ran {v['run_time']*1000:.1f}ms without yielding")
    else:
        print("starvation violations: none")


if __name__ == "__main__":
    main()
