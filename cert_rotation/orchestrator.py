"""证书轮换编排器：状态机 + 预写日志（WAL），崩溃可恢复、可回滚。

状态机：

    IDLE --start--> PREPARED --pump--> SHIFTING --pump...--> COMPLETE --pump--> RETIRED
        任意中间状态 --rollback--> ROLLING_BACK --> IDLE

安全不变量（由状态转移顺序保证）：
1. 安装新证书 -> 2. 逐步把流量从旧证书切到新证书 -> 3. 流量 0% 后才下线旧证书。
   任何时刻至少有一张**有效**证书在承载 100% 的新握手流量，
   不存在“两套都不生效”的窗口。
4. 每个状态转移先写日志（原子 rename）再改网关；崩溃后重启按日志重放，
   与真实部署状态对账（reconcile）后继续或回退。
5. 回滚是对称的逆操作：流量先切回旧证书，再下线新证书；旧证书在回滚
   期间始终安装，因此回滚中断重启后旧证书依然生效。
"""

from __future__ import annotations

import json
import os
from typing import Callable, Dict, List, Optional

from .model import Certificate, Clock, Gateway

# ---- 状态 ----
IDLE = "IDLE"
PREPARED = "PREPARED"
SHIFTING = "SHIFTING"
COMPLETE = "COMPLETE"
RETIRED = "RETIRED"
ROLLING_BACK = "ROLLING_BACK"

# ---- 崩溃注入钩子（fault hook）可观察的事件点 ----
EV_PREPARED = "prepared"            # 新证书已安装、状态已置 PREPARED
EV_SHIFT_APPLIED = "shift_applied"  # 某档流量已切换并落盘
EV_COMPLETE = "complete"            # 流量已 100% 到新证书
EV_RETIRED = "retired"              # 旧证书已下线（终态）
EV_ROLLBACK_STARTED = "rollback_started"
EV_ROLLBACK_DONE = "rollback_done"


class RotationError(Exception):
    """轮换失败（如部署校验失败、证书无效）。"""


class Journal:
    """单文件 WAL：state.json 的原子替换写。"""

    def __init__(self, path: str) -> None:
        self.path = path

    def load(self) -> Optional[dict]:
        if not os.path.exists(self.path):
            return None
        with open(self.path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def save(self, state: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)  # 原子提交


def _serial_of(cert: Optional[Certificate]) -> Optional[str]:
    return cert.serial if cert else None


class Orchestrator:
    """轮换编排器。

    参数：
        gateway   被编排的网关（控制面 + 数据面模拟）
        journal   WAL 日志
        clock     可注入时钟
        on_event  崩溃注入钩子：fn(event_name) -> None，抛出即模拟崩溃
        deploy_fail_at  模拟部署失败：pct_old 等于该值时 set_split 抛错
    """

    ROLLBACKABLE = (PREPARED, SHIFTING, COMPLETE)

    def __init__(
        self,
        gateway: Gateway,
        journal: Journal,
        clock: Clock,
        on_event: Optional[Callable[[str], None]] = None,
        deploy_fail_at: Optional[int] = None,
    ) -> None:
        self.gateway = gateway
        self.journal = journal
        self.clock = clock
        self.on_event = on_event
        self.deploy_fail_at = deploy_fail_at
        self._state: dict = self.journal.load() or {
            "state": IDLE,
            "old": None,
            "new": None,
            "plan": [],
            "step": 0,
            "emergency": False,
            "certs": {},
            "events": [],
        }
        # 时间线随 WAL 持久化，重启后连续完整。
        self.timeline: List[dict] = self._state.setdefault("events", [])
        self._certs: Dict[str, Certificate] = {
            serial: Certificate(serial, meta["not_before"], meta["not_after"])
            for serial, meta in self._state.get("certs", {}).items()
        }
        self._reconcile()

    # ---------- 基础 ----------

    @property
    def state(self) -> str:
        return self._state["state"]

    def _emit(self, kind: str, **fields) -> None:
        event = {"t": self.clock.now(), "event": kind, "state": self.state}
        event.update(fields)
        self.timeline.append(event)
        self.journal.save(self._state)  # 事件落盘，崩溃不丢时间线

    def _commit(self, **changes) -> None:
        """先落盘（WAL），再让内存状态生效。"""
        self._state.update(changes)
        self.journal.save(self._state)

    def _fire(self, event: str) -> None:
        if self.on_event:
            self.on_event(event)

    def _cert(self, serial: Optional[str]) -> Optional[Certificate]:
        return self._certs.get(serial) if serial else None

    # ---------- 启动 / 恢复 ----------

    def _reconcile(self) -> None:
        """重启后对账：让网关实际配置与日志中的控制面状态一致。

        只移除“多余”的证书、只把流量比例对齐到日志记录值，
        因此无论崩在哪个缝隙，都不会出现两套证书都不生效的窗口。
        """
        st = self._state
        state = st["state"]
        old_s, new_s = st["old"], st["new"]
        gw = self.gateway

        if state in (PREPARED, SHIFTING, COMPLETE, ROLLING_BACK):
            want = {s for s in (old_s, new_s) if s}
            if state == ROLLING_BACK:
                want = {old_s} if old_s else set()
            # 顺序很重要：先补装日志要求在、实际缺失的证书，
            # 再对齐流量比例（保证目标证书在位），最后摘除多余证书。
            for serial in want:
                if serial not in gw.installed and serial in self._certs:
                    gw.install(self._certs[serial])
            if state == ROLLING_BACK:
                gw.set_split(100, old_s, new_s or old_s)
            else:
                gw.set_split(self._current_pct(), old_s, new_s)
            for serial in list(gw.installed):
                if serial not in want:
                    gw.remove(serial)
        elif state == RETIRED:
            # RETIRED 后 new 是唯一生效证书：先对齐比例再摘旧证书。
            if new_s:
                gw.set_split(0, new_s, new_s)
                for serial in list(gw.installed):
                    if serial != new_s:
                        gw.remove(serial)
            # IDLE：保持网关现状（尚未开始或已回滚完成）

    def _current_pct(self) -> int:
        st = self._state
        if st["state"] == PREPARED or st["step"] == 0:
            return 100  # 尚未应用任何切换档位
        if st["state"] == COMPLETE or st["step"] >= len(st["plan"]):
            return 0
        return st["plan"][st["step"] - 1][1]  # 最后一档已应用的比例

    # ---------- 对外 API ----------

    def start(
        self,
        old: Certificate,
        new: Certificate,
        shift_plan: List[tuple],
        emergency: bool = False,
    ) -> None:
        """发起轮换。shift_plan 为 [(相对开始秒, 旧流量百分比), ...]，
        最后一档应为 0。emergency=True 表示旧证书已过期，立即全量切换。"""
        if self.state != IDLE:
            raise RotationError(f"cannot start rotation in state {self.state}")
        now = self.clock.now()
        if not new.valid_at(now):
            self._emit("start_rejected", reason="new certificate not valid",
                       new=new.serial)
            raise RotationError("new certificate is not valid; rotation aborted")
        if not old.valid_at(now) and not emergency:
            self._emit("start_rejected", reason="old certificate expired",
                       old=old.serial)
            raise RotationError(
                "old certificate already expired; use emergency rotation"
            )
        if old.serial not in self.gateway.installed:
            raise RotationError("old certificate is not installed on gateway")
        if not emergency and (not shift_plan or shift_plan[-1][1] != 0):
            raise RotationError("shift plan must end at pct_old=0")

        self._certs[old.serial] = old
        self._certs[new.serial] = new
        abs_plan = [(now + offset, pct) for offset, pct in shift_plan]
        self._emit("rotation_started", old=old.serial, new=new.serial,
                   emergency=emergency, plan=abs_plan)

        # WAL：先记录意图，再安装新证书，再进入 PREPARED。
        certs = {
            c.serial: {"not_before": c.not_before, "not_after": c.not_after}
            for c in (old, new)
        }
        self._commit(state=PREPARED, old=old.serial, new=new.serial,
                     plan=abs_plan, step=0, emergency=emergency, certs=certs)
        self.gateway.install(new)
        self.gateway.set_split(100, old.serial, new.serial)
        self._emit("new_cert_installed", new=new.serial)
        self._fire(EV_PREPARED)

    def pump(self) -> str:
        """处理到期的状态转移；返回当前状态。崩溃安全、可重入。"""
        st = self._state
        state = st["state"]
        if state == PREPARED:
            self._commit(state=SHIFTING)
            self._emit("shifting_began")
            state = SHIFTING
        if state == SHIFTING:
            self._pump_shifting()
        elif state == COMPLETE:
            self._retire_old()
        elif state == ROLLING_BACK:
            self._finish_rollback()
        return self.state

    def rollback(self, reason: str = "manual") -> None:
        """回滚：旧证书重新承载 100% 流量，新证书下线。"""
        if self.state not in self.ROLLBACKABLE:
            raise RotationError(f"cannot rollback in state {self.state}")
        self._commit(state=ROLLING_BACK)
        self._emit("rollback_started", reason=reason)
        self._fire(EV_ROLLBACK_STARTED)
        self._apply_rollback()

    # ---------- 内部步骤 ----------

    def _apply_split(self, pct: int) -> None:
        if self.deploy_fail_at is not None and pct == self.deploy_fail_at:
            raise RotationError(f"deploy failed at pct_old={pct}")
        self.gateway.set_split(pct, self._state["old"], self._state["new"])

    def _pump_shifting(self) -> None:
        st = self._state
        now = self.clock.now()
        plan = st["plan"]
        step = st["step"]
        moved = False
        while step < len(plan) and plan[step][0] <= now:
            pct = plan[step][1]
            self._apply_split(pct)  # 失败向上抛，状态留在 SHIFTING，可回滚
            step += 1
            self._commit(step=step)
            self._emit("traffic_shifted", pct_old=pct,
                       pct_new=100 - pct)
            self._fire(EV_SHIFT_APPLIED)
            moved = True
        if step >= len(plan):
            self._commit(state=COMPLETE)
            self._emit("shift_complete", pct_old=0, pct_new=100)
            self._fire(EV_COMPLETE)
        elif not moved and st["emergency"]:
            # 紧急轮换：旧证书已过期，立即全量切换，不等计划时间。
            self._apply_split(0)
            self._commit(step=len(plan), state=COMPLETE)
            self._emit("emergency_cutover", pct_old=0, pct_new=100)
            self._fire(EV_COMPLETE)

    def _retire_old(self) -> None:
        st = self._state
        old_s = st["old"]
        # 顺序：先确认流量 0% 在旧证书（COMPLETE 已保证），再下线旧证书。
        self.gateway.remove(old_s)
        self._commit(state=RETIRED)
        self._emit("old_cert_retired", old=old_s)
        self._fire(EV_RETIRED)

    def _apply_rollback(self) -> None:
        st = self._state
        old_s, new_s = st["old"], st["new"]
        # 1. 流量先全部切回旧证书（旧证书一直安装着）。
        self.gateway.set_split(100, old_s, new_s)
        self._emit("traffic_restored_to_old", pct_old=100, pct_new=0)
        # 2. 新证书下线。
        if new_s in self.gateway.installed:
            self.gateway.remove(new_s)
        self._commit(state=IDLE, new=None, plan=[], step=0, emergency=False)
        self._emit("rollback_done", old=old_s)
        self._fire(EV_ROLLBACK_DONE)

    def _finish_rollback(self) -> None:
        """ROLLING_BACK 状态下重启/重入：对账后幂等完成回滚。"""
        self._apply_rollback()

    # ---------- 时间线 ----------

    def render_timeline(self, t0: Optional[float] = None) -> str:
        base = self.timeline[0]["t"] if t0 is None and self.timeline else (t0 or 0.0)
        lines = []
        for ev in self.timeline:
            extra = {k: v for k, v in ev.items() if k not in ("t", "event", "state")}
            suffix = " " + json.dumps(extra, ensure_ascii=False) if extra else ""
            lines.append(f"t+{ev['t'] - base:7.1f}s [{ev['state']:<12}] {ev['event']}{suffix}")
        return "\n".join(lines)
