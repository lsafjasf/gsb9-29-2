"""Pre-fix scheduler: strict static priority, no aging.

Kept only to reproduce the starvation bug and as a regression reference.
It always dispatches the highest-base-priority ready coroutine, so a ready
low-priority coroutine runs only after every higher-priority coroutine has
finished.  Under sustained high-priority load its wait is unbounded: it gets
CPU time only when the load drops.
"""

from scheduler import Scheduler


class StrictPriorityScheduler(Scheduler):
    """Always dispatches the highest-base-priority ready coroutine (FIFO)."""

    def __init__(self, num_priorities=8, time_slice=1):
        super().__init__(num_priorities=num_priorities, aging_interval=1,
                         time_slice=time_slice)

    def effective_priority(self, task):  # no aging: wait time is ignored
        return task.priority
