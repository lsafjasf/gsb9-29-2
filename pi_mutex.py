"""优先级继承互斥锁 + 确定性滴答调度器（仅标准库）。

任务体是一个迭代器，逐条产出操作（op）：
    ("work", ticks)             占用 CPU ticks 个调度滴答
    ("sleep", ticks)            离开就绪集合 ticks 个滴答
    ("lock", mutex[, timeout])  申请互斥锁，可选超时（滴答数）
    ("unlock", mutex)           释放互斥锁

调度器每个滴答选择有效优先级最高的就绪任务运行（同优先级按就绪先后 FIFO）。
有效优先级 = 基础优先级 + 继承提升量（boost）。
"""

class Task:
    def __init__(self, name, prio, body):
        self.name = name
        self.base_prio = prio
        self.boost = 0                 # 继承得到的提升量
        self.body = iter(body)
        self.state = "ready"           # ready | blocked | sleeping | done
        self.work_left = 0
        self.sleep_until = 0
        self.waiting_on = None         # 正在等待的 Mutex
        self.deadline = None           # 等待超时时刻（滴答）
        self.blocked_since = None
        self.wait_seq = 0              # 进入等待的顺序号（FIFO 平局裁决）
        self.ready_seq = 0             # 进入就绪的顺序号
        self.total_wait = 0            # 累计阻塞滴答数（量化指标）
        self.held = []                 # 当前持有的 Mutex 列表
        self.lock_results = []         # 每次 lock 的结果 True/False(超时)
        self.finished_at = None

    @property
    def eff_prio(self):
        return self.base_prio + self.boost

    def __repr__(self):
        return f"Task({self.name}, base={self.base_prio}, eff={self.eff_prio})"


class Mutex:
    def __init__(self, name):
        self.name = name
        self.holder = None
        self.waiters = []

    def __repr__(self):
        return f"Mutex({self.name})"


class Scheduler:
    def __init__(self, inheritance=True):
        self.inheritance = inheritance
        self.now = 0
        self.tasks = []
        self.events = []               # (now, kind, ...) 事件轨迹，供断言
        self._seq = 0

    def add(self, task, start_at=0):
        if start_at > 0:
            task.state = "sleeping"
            task.sleep_until = start_at
        else:
            task.ready_seq = self._next_seq()
        self.tasks.append(task)
        return task

    def _next_seq(self):
        self._seq += 1
        return self._seq

    # ---- 提升（boost）计算 -------------------------------------------------
    def recompute_boost(self, task):
        """从 task 起沿等待链向上重算 boost。

        有效优先级 = max(基础优先级, 其持有的所有锁的等待者的有效优先级)。
        若 task 自身也阻塞在别的锁上（嵌套持有），变化会沿 holder 链向上传播。
        """
        cur = task
        seen = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            wait_max = 0
            for m in cur.held:
                for w in m.waiters:
                    if w.eff_prio > wait_max:
                        wait_max = w.eff_prio
            new_boost = max(0, wait_max - cur.base_prio) if self.inheritance else 0
            if new_boost == cur.boost:
                break                  # 未变化则上游不受影响，停止传播
            self.events.append((self.now, "boost", cur.name, cur.boost, new_boost))
            cur.boost = new_boost
            cur = cur.waiting_on.holder if cur.waiting_on is not None else None

    # ---- 加锁 / 解锁 -------------------------------------------------------
    def _lock(self, task, mtx, timeout):
        if mtx.holder is None:
            mtx.holder = task
            task.held.append(mtx)
            task.lock_results.append(True)
            self.events.append((self.now, "lock", task.name, mtx.name))
            return
        if mtx.holder is task:
            raise RuntimeError(f"{task.name}: 重复加锁非递归互斥锁 {mtx.name}")
        mtx.waiters.append(task)
        task.state = "blocked"
        task.waiting_on = mtx
        task.blocked_since = self.now
        task.wait_seq = self._next_seq()
        task.deadline = None if timeout is None else self.now + timeout
        self.events.append((self.now, "block", task.name, mtx.name))
        # 判定顺序 1：等待者入队后，立即从持有者起向上重算提升
        self.recompute_boost(mtx.holder)

    def _unlock(self, task, mtx):
        if mtx.holder is not task:
            raise RuntimeError(f"{task.name}: 释放未持有的锁 {mtx.name}")
        mtx.holder = None
        task.held.remove(mtx)
        self.events.append((self.now, "unlock", task.name, mtx.name))
        # 判定顺序 2：先恢复释放者自身的 boost（可能只降不归零，若仍持有其他被等待的锁）
        self.recompute_boost(task)
        if mtx.waiters:
            # 判定顺序 3：在等待者中选有效优先级最高者（平局按等待先后 FIFO）直接交接锁
            nxt = max(mtx.waiters, key=lambda w: (w.eff_prio, -w.wait_seq))
            mtx.waiters.remove(nxt)
            mtx.holder = nxt
            nxt.held.append(mtx)
            nxt.state = "ready"
            nxt.ready_seq = self._next_seq()
            nxt.total_wait += self.now - nxt.blocked_since
            nxt.waiting_on = None
            nxt.deadline = None
            nxt.lock_results.append(True)
            self.events.append((self.now, "wake", nxt.name, mtx.name))
            # 判定顺序 4：新持有者可能从剩余等待者继承提升
            self.recompute_boost(nxt)

    # ---- 超时与睡眠 --------------------------------------------------------
    def _handle_timeouts(self):
        for t in self.tasks:
            if t.state == "blocked" and t.deadline is not None and self.now >= t.deadline:
                mtx = t.waiting_on
                mtx.waiters.remove(t)
                t.waiting_on = None
                t.deadline = None
                t.state = "ready"
                t.ready_seq = self._next_seq()
                t.total_wait += self.now - t.blocked_since
                t.lock_results.append(False)
                self.events.append((self.now, "timeout", t.name, mtx.name))
                # 等待者退出后，持有者提升需相应回收
                if mtx.holder is not None:
                    self.recompute_boost(mtx.holder)

    def _wake_sleepers(self):
        for t in self.tasks:
            if t.state == "sleeping" and self.now >= t.sleep_until:
                t.state = "ready"
                t.ready_seq = self._next_seq()

    # ---- 主循环 ------------------------------------------------------------
    def _step(self, task):
        if task.work_left > 0:
            task.work_left -= 1
            self.now += 1
            return
        try:
            op = next(task.body)
        except StopIteration:
            task.state = "done"
            task.finished_at = self.now
            return
        kind = op[0]
        if kind == "work":
            task.work_left = op[1]
        elif kind == "sleep":
            task.state = "sleeping"
            task.sleep_until = self.now + op[1]
        elif kind == "lock":
            self._lock(task, op[1], op[2] if len(op) > 2 else None)
        elif kind == "unlock":
            self._unlock(task, op[1])
        else:
            raise ValueError(f"未知操作 {op!r}")

    def run(self, max_ticks=1_000_000):
        steps = 0
        while any(t.state != "done" for t in self.tasks):
            self._wake_sleepers()
            self._handle_timeouts()
            ready = [t for t in self.tasks if t.state == "ready"]
            if not ready:
                future = [t.sleep_until for t in self.tasks if t.state == "sleeping"]
                future += [t.deadline for t in self.tasks
                           if t.state == "blocked" and t.deadline is not None]
                if not future:
                    raise RuntimeError("死锁：无可运行任务且无未来事件")
                self.now = min(future)
                continue
            task = max(ready, key=lambda t: (t.eff_prio, -t.ready_seq))
            self._step(task)
            steps += 1
            if self.now > max_ticks or steps > max_ticks * 4:
                raise RuntimeError("超过滴答上限")
        return self.now
