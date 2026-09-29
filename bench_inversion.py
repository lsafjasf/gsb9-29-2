"""量化对比：优先级继承开启/关闭时，高优先级任务的等待时长。

场景（经典优先级反转）：
    low  (prio 1, t=0 到达)：持锁 M，临界区 30 滴答
    med  (prio 5, t=2 到达)：纯计算 50 滴答，不碰锁
    high (prio 10, t=4 到达)：需要锁 M，临界区 5 滴答
"""
from pi_mutex import Task, Mutex, Scheduler


def run_case(inheritance):
    sch = Scheduler(inheritance=inheritance)
    M = Mutex("M")
    low = Task("low", 1, [("lock", M), ("work", 30), ("unlock", M)])
    med = Task("med", 5, [("work", 50)])
    high = Task("high", 10, [("lock", M), ("work", 5), ("unlock", M)])
    sch.add(low, start_at=0)
    sch.add(med, start_at=2)
    sch.add(high, start_at=4)
    sch.run()
    return high, med, low


def main():
    off_high, off_med, off_low = run_case(inheritance=False)
    on_high, on_med, on_low = run_case(inheritance=True)

    print("场景：low 持锁 30t / med 占 CPU 50t / high 等锁（high 于 t=4 阻塞）")
    print()
    header = f"{'配置':<16}{'high 等待(ticks)':>16}{'high 完成时刻':>14}{'med 完成时刻':>13}"
    print(header)
    print("-" * len(header))
    print(f"{'继承关闭':<16}{off_high.total_wait:>16}{off_high.finished_at:>14}{off_med.finished_at:>13}")
    print(f"{'继承开启':<16}{on_high.total_wait:>16}{on_high.finished_at:>14}{on_med.finished_at:>13}")
    print()
    speedup = off_high.total_wait / on_high.total_wait
    print(f"高优先级等待时长降低 {off_high.total_wait - on_high.total_wait} ticks"
          f"（{speedup:.2f}x），完成时刻提前 {off_high.finished_at - on_high.finished_at} ticks")
    print(f"med 完成时刻延后 {on_med.finished_at - off_med.finished_at} ticks"
          "（预期行为：中优先级让位于被提升的临界区，高优先级工作整体提前）")


if __name__ == "__main__":
    main()
