"""量化对比：优先级继承开启前后，高优先级线程的等待时长。

运行：python3 demo.py
"""

from priority_inheritance import Mutex, Scheduler, Thread, lock, unlock, work


def run_classic(pi_enabled, medium_work):
    """L(1) 持锁工作 8 tick；M(2) 忙 medium_work tick；H(3) 等同一把锁。"""
    m = Mutex("M")

    def low():
        yield lock(m)
        yield work(8)
        yield unlock(m)

    def med():
        yield work(medium_work)

    def high():
        yield lock(m)
        yield work(2)
        yield unlock(m)

    sched = Scheduler(pi_enabled)
    sched.add_thread(Thread("L", 1, low()))
    sched.add_thread(Thread("M", 2, med(), start_at=1))
    h = sched.add_thread(Thread("H", 3, high(), start_at=2))
    sched.run()
    return h.last_wait


def run_unbounded(pi_enabled, max_ticks=100_000):
    """中优先级线程永久占 CPU（直到 H 完成才停）：关闭 PI 时 H 无限期阻塞。"""
    m = Mutex("M")
    stop = {"done": False}

    def low():
        yield lock(m)
        yield work(8)
        yield unlock(m)

    def med():
        while not stop["done"]:
            yield work(1)

    def high():
        yield lock(m)
        yield work(2)
        yield unlock(m)
        stop["done"] = True

    sched = Scheduler(pi_enabled)
    sched.add_thread(Thread("L", 1, low()))
    sched.add_thread(Thread("M", 2, med(), start_at=1))
    h = sched.add_thread(Thread("H", 3, high(), start_at=2))
    try:
        sched.run(max_ticks=max_ticks)
        return h.last_wait
    except RuntimeError:
        return float("inf")   # 模拟 max_ticks 后 H 仍未获锁


def fmt(wait):
    return "∞（无限期阻塞）" if wait == float("inf") else f"{wait} tick"


def main():
    print("场景：L(优先级1) 持锁；M(优先级2) 持续占 CPU；H(优先级3) 等 L 的锁")
    print("-" * 64)
    print(f"{'场景':<28}{'PI 关闭':<18}{'PI 开启':<10}")
    for n in (100, 1000, 5000):
        off = run_classic(pi_enabled=False, medium_work=n)
        on = run_classic(pi_enabled=True, medium_work=n)
        print(f"{'M 忙 ' + str(n) + ' tick':<28}{fmt(off):<18}{fmt(on):<10}")
    off = run_unbounded(pi_enabled=False)
    on = run_unbounded(pi_enabled=True)
    print(f"{'M 永久占 CPU':<28}{fmt(off):<18}{fmt(on):<10}")
    print("-" * 64)
    print("结论：PI 开启后，H 的等待只取决于 L 的剩余临界区（约 9 tick），")
    print("与中优先级线程的工作量完全解耦。")


if __name__ == "__main__":
    main()
