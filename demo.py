"""演示：公平调度 + 饥饿检测 + 睡眠/唤醒 + 统计输出。

运行：python3 demo.py
"""

import time

from coop_sched import Scheduler, Sleep


def worker(tid, steps):
    for i in range(steps):
        yield  # 让步，轮流执行


def hog():
    # 恶意/笨拙协程：150ms 不让步，应被饥饿检测捕获
    deadline = time.monotonic() + 0.15
    while time.monotonic() < deadline:
        pass
    yield


def sleeper(log):
    log.append(("sleep-start", time.monotonic()))
    yield Sleep(5.0)  # 打算睡 5s，但会被提前唤醒
    log.append(("woken", time.monotonic()))


def waker(target):
    yield Sleep(0.05)  # 50ms 后唤醒 sleeper
    target.wake()


def main():
    log = []
    sched = Scheduler(starvation_threshold=0.05)

    for i in range(5):
        sched.spawn(worker, i, 4, name=f"worker-{i}")
    sched.spawn(hog, name="hog")
    task_sleeper = sched.spawn(sleeper, log, name="sleeper")
    sched.spawn(waker, task_sleeper, name="waker")

    stats = sched.run()
    print(stats.summary())

    slept_for = log[1][1] - log[0][1]
    print(f"\nsleeper 实际睡眠 {slept_for * 1000:.1f}ms（请求 5000ms，被 waker 提前唤醒）")


if __name__ == "__main__":
    main()
