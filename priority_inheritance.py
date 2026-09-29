"""优先级继承（Priority Inheritance, PI）模拟库 —— 仅依赖 Python 标准库。

模型：单处理器、抢占式、离散 tick 调度器 + 支持优先级继承的互斥锁。
真实 CPython 线程无法设置调度优先级，因此这里用确定性的 tick 模拟来
精确复现并度量"低优先级持锁、中优先级占 CPU、高优先级被无限阻塞"的
优先级反转场景。

线程体是一个生成器，yield 下列操作元组（可用下方同名辅助函数构造）：
    ('work', n)              占用 CPU n 个 tick
    ('sleep', n)             睡眠 n 个 tick（不可被调度，也不占 CPU）
    ('lock', mutex)          申请锁（无限等待），被唤醒时收到 True
    ('lock', mutex, timeout) 申请锁（最多等 timeout tick），唤醒时收到 True/False
    ('unlock', mutex)        释放锁

优先级继承规则：
  * 线程有效优先级 = max(基础优先级, 其持有的每把锁的等待者的有效优先级)。
  * 沿"等待链"传递（A 等 B 持有的锁、B 等 C 持有的锁 => C 也被提升），
    通过不动点迭代计算。
  * 释放某把锁 / 等待者超时退出后重算：持有者仍被其它持有锁的等待者提升时
    只降不复原，全部等待关系消失后才恢复基础优先级。
"""

__all__ = ["Mutex", "Thread", "Scheduler", "work", "sleep", "lock", "unlock"]


# ---------------------------------------------------------------- 操作构造

def work(n):
    return ("work", n)


def sleep(n):
    return ("sleep", n)


def lock(mutex, timeout=None):
    if timeout is None:
        return ("lock", mutex)
    return ("lock", mutex, timeout)


def unlock(mutex):
    return ("unlock", mutex)


# ---------------------------------------------------------------- 核心对象

class Mutex:
    def __init__(self, name):
        self.name = name
        self.owner = None          # 当前持有者 Thread 或 None
        self.waiters = {}          # Thread -> 超时截止 tick（None 表示无限等待）

    def __repr__(self):
        return f"<Mutex {self.name}>"


class Thread:
    def __init__(self, name, prio, body, start_at=0):
        self.name = name
        self.base_prio = prio      # 基础（原始）优先级，数值越大越高
        self.eff_prio = prio       # 有效优先级（被提升后可能大于基础值）
        self.gen = body            # 线程体生成器
        self.start_at = start_at   # 最早可被调度的 tick（绝对时刻）
        self.held = set()          # 当前持有的锁
        self.blocked_on = None     # 正在等待的锁
        self.sleep_until = 0
        self.ready_since = 0
        self.work_left = 0
        self.pending_op = None
        self.done = False
        self.wait_start = None     # 本次阻塞开始的 tick
        self.last_wait = None      # 最近一次等待时长（tick）
        self.total_wait = 0
        self._reported_prio = prio

    def __repr__(self):
        return f"<Thread {self.name} prio={self.base_prio}/{self.eff_prio}>"


class Scheduler:
    """单处理器抢占式 tick 调度器。pi_enabled=False 时关闭优先级继承（对照组）。"""

    def __init__(self, pi_enabled=True):
        self.pi_enabled = pi_enabled
        self.threads = []
        self.mutexes = set()
        self.now = 0
        # 事件日志：(tick, kind, ...)，kind ∈ boost/restore/block/grant/release/timeout
        self.events = []

    # ------------------------------------------------------------ 基础设施

    def add_thread(self, thread):
        try:
            thread.pending_op = next(thread.gen)
        except StopIteration:
            thread.done = True
        thread.ready_since = self.now
        self.threads.append(thread)
        return thread

    def _advance(self, t, result):
        """把锁申请结果送回线程体，取下一条操作。"""
        try:
            t.pending_op = t.gen.send(result)
        except StopIteration:
            t.done = True
            t.pending_op = None

    # ---------------------------------------------------- 优先级继承（核心）

    def recompute(self):
        """重算所有线程的有效优先级（不动点迭代 => 支持等待链传递与嵌套持有）。

        恢复判定顺序：先全部回落到基础优先级，再沿"持有者 <- 等待者"的边
        反复取 max 直到不动点。因此释放一把锁后，若持有者仍持有其它有等待者
        的锁，它只降到"剩余等待者的最高优先级"，而非直接降回基础值。
        """
        for t in self.threads:
            t.eff_prio = t.base_prio
        if self.pi_enabled:
            changed = True
            while changed:
                changed = False
                for t in self.threads:
                    for m in t.held:
                        for w in m.waiters:
                            if w.eff_prio > t.eff_prio:
                                t.eff_prio = w.eff_prio
                                changed = True
        # 只在净变化时记录一次事件，避免迭代中间态刷屏
        for t in self.threads:
            if t.eff_prio != t._reported_prio:
                kind = "boost" if t.eff_prio > t._reported_prio else "restore"
                self.events.append((self.now, kind, t.name,
                                    t._reported_prio, t.eff_prio))
                t._reported_prio = t.eff_prio

    # ------------------------------------------------------------ 锁操作

    def _grant(self, m, t):
        m.owner = t
        t.held.add(m)

    def _release(self, m, t):
        t.held.discard(m)
        m.owner = None
        self.events.append((self.now, "release", m.name, t.name))
        # 先重算（持有者可能因其它持有锁仍被提升），再按等待者有效优先级选继承者
        self.recompute()
        if m.waiters:
            w = max(m.waiters, key=lambda x: (x.eff_prio, -x.wait_start))
            del m.waiters[w]
            w.blocked_on = None
            w.ready_since = self.now
            w.last_wait = self.now - w.wait_start
            w.total_wait += w.last_wait
            self._grant(m, w)
            self.events.append((self.now, "grant", m.name, w.name))
            self._advance(w, True)
            self.recompute()

    def _expire_timeouts(self):
        """超时等待者退出队列并收到 False，持有者相应地被取消提升。"""
        for m in list(self.mutexes):
            for w, deadline in list(m.waiters.items()):
                if deadline is not None and self.now >= deadline:
                    del m.waiters[w]
                    w.blocked_on = None
                    w.ready_since = self.now
                    w.last_wait = self.now - w.wait_start
                    w.total_wait += w.last_wait
                    self.events.append((self.now, "timeout", m.name, w.name))
                    self._advance(w, False)
        self.recompute()

    # ------------------------------------------------------------ 单步执行

    def _step(self, t):
        if t.work_left > 0:
            t.work_left -= 1
            if t.work_left == 0:
                self._advance(t, None)
            return
        op = t.pending_op
        kind = op[0]
        if kind == "work":
            n = op[1]
            if n > 0:
                t.work_left = n - 1
                if t.work_left == 0:
                    self._advance(t, None)
            else:
                self._advance(t, None)
        elif kind == "sleep":
            t.sleep_until = self.now + op[1]
            self._advance(t, None)
        elif kind == "lock":
            m = op[1]
            self.mutexes.add(m)
            timeout = op[2] if len(op) > 2 else None
            if m.owner is None:
                self._grant(m, t)
                self.events.append((self.now, "grant", m.name, t.name))
                self._advance(t, True)
                self.recompute()
            else:
                m.waiters[t] = (self.now + timeout) if timeout is not None else None
                t.blocked_on = m
                t.wait_start = self.now
                self.events.append((self.now, "block", m.name, t.name))
                self.recompute()   # 触发提升
        elif kind == "unlock":
            m = op[1]
            assert m.owner is t, f"{t.name} 不持有 {m.name}，无法释放"
            self._release(m, t)
            self._advance(t, None)
        else:
            raise ValueError(f"未知操作: {op}")

    # ------------------------------------------------------------ 主循环

    def _runnable(self):
        return [t for t in self.threads
                if not t.done and t.blocked_on is None
                and t.sleep_until <= self.now and t.start_at <= self.now]

    def _next_event_tick(self):
        cands = [t.sleep_until for t in self.threads
                 if not t.done and t.blocked_on is None and t.sleep_until > self.now]
        cands += [t.start_at for t in self.threads
                  if not t.done and t.start_at > self.now]
        for m in self.mutexes:
            cands += [d for d in m.waiters.values() if d is not None and d > self.now]
        return min(cands) if cands else None

    def run(self, max_ticks=1_000_000):
        while not all(t.done for t in self.threads):
            if self.now >= max_ticks:
                raise RuntimeError(
                    f"超过 max_ticks={max_ticks}：疑似死锁或无限期阻塞")
            self._expire_timeouts()
            self.recompute()
            runnable = self._runnable()
            if not runnable:
                nxt = self._next_event_tick()
                if nxt is None:
                    raise RuntimeError("死锁：所有线程都在无限期等待")
                self.now = nxt
                continue
            # 抢占式：每个 tick 重新选择有效优先级最高者（同优先级先到先服务）
            t = min(runnable, key=lambda x: (-x.eff_prio, x.ready_since))
            self._step(t)
            self.now += 1
        self.recompute()
        return self.now
