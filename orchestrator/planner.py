"""规划：依赖校验、循环检测（报完整环路径）、资源受限调度。"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Any, Dict, List, Set

from .model import Resources, Step


class PlanError(ValueError):
    pass


class CyclicDependencyError(PlanError):
    def __init__(self, cycle: List[str]) -> None:
        self.cycle = cycle                      # 含起点收尾的完整环
        super().__init__("检测到循环依赖: " + " -> ".join(cycle))


@dataclass
class Plan:
    order: List[str] = field(default_factory=list)     # 确定性派发顺序（拓扑序）
    waves: List[List[str]] = field(default_factory=list)
    timeline: List[Dict[str, Any]] = field(default_factory=list)
    estimated_makespan: float = 0.0


class Planner:
    def __init__(self, steps: List[Step], limits: Dict[str, float]) -> None:
        self.steps = {s.id: s for s in steps}
        if len(self.steps) != len(steps):
            raise PlanError("步骤 id 重复")
        self.limits = limits
        self._validate()

    # ---- 校验 ----
    def _validate(self) -> None:
        for step in self.steps.values():
            for dep in step.requires:
                if dep not in self.steps:
                    raise PlanError("步骤 %r 依赖了不存在的步骤 %r" % (step.id, dep))
            for key, need in step.resources.items():
                if need < 0:
                    raise PlanError("步骤 %r 的资源占用为负: %s" % (step.id, key))
                if need > self.limits.get(key, 0.0) + 1e-9:
                    raise PlanError(
                        "步骤 %r 的 %s 需求 %g 超过上限 %g"
                        % (step.id, key, need, self.limits.get(key, 0.0)))
        self._find_cycle()

    def _find_cycle(self) -> None:
        """DFS 找环；找到后回溯出完整环路径 v0 -> ... -> v0。"""
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {sid: WHITE for sid in self.steps}
        stack: List[str] = []

        def dfs(node: str) -> None:
            color[node] = GRAY
            stack.append(node)
            for dep in sorted(self.steps[node].requires):
                if color[dep] == GRAY:
                    idx = stack.index(dep)
                    raise CyclicDependencyError(stack[idx:] + [dep])
                if color[dep] == WHITE:
                    dfs(dep)
            stack.pop()
            color[node] = BLACK

        for sid in sorted(self.steps):
            if color[sid] == WHITE:
                dfs(sid)

    # ---- 确定性拓扑序 ----
    def _topo_order(self) -> List[str]:
        indeg: Dict[str, int] = {sid: 0 for sid in self.steps}
        children: Dict[str, List[str]] = {sid: [] for sid in self.steps}
        for step in self.steps.values():
            for dep in step.requires:
                indeg[step.id] += 1
                children[dep].append(step.id)
        heap: List[str] = [sid for sid, d in indeg.items() if d == 0]
        heapq.heapify(heap)
        order: List[str] = []
        while heap:
            sid = heapq.heappop(heap)
            order.append(sid)
            for child in children[sid]:
                indeg[child] -= 1
                if indeg[child] == 0:
                    heapq.heappush(heap, child)
        return order  # _validate 已保证无环

    # ---- 执行计划：事件驱动列表调度（优先级 = 拓扑序位置）----
    def make_plan(self) -> Plan:
        order = self._topo_order()
        rank = {sid: i for i, sid in enumerate(order)}
        indeg: Dict[str, int] = {
            s.id: len(s.requires) for s in self.steps.values()}
        completed: Set[str] = set()
        running: List[Dict[str, Any]] = []   # {id, finish}
        used: Dict[str, float] = {key: 0.0 for key in self.limits}
        pending = set(order)
        now = 0.0
        timeline: List[Dict[str, Any]] = []
        waves: List[List[str]] = []

        while pending or running:
            # 按拓扑优先级依次派发所有资源允许的就绪步骤
            wave: List[str] = []
            progressed = False
            while True:
                ready = [sid for sid in order
                         if sid in pending and indeg[sid] == 0]
                started_any = False
                for sid in ready:
                    need = self.steps[sid].resources
                    if Resources.fits(used, need, self.limits):
                        for key, value in need.items():
                            used[key] = used.get(key, 0.0) + value
                        running.append({
                            "id": sid,
                            "finish": now + max(
                                0.0, self.steps[sid].duration_estimate),
                        })
                        timeline.append({
                            "id": sid, "wave": len(waves),
                            "start": round(now, 6),
                            "finish": round(
                                now + self.steps[sid].duration_estimate, 6),
                        })
                        wave.append(sid)
                        pending.discard(sid)
                        started_any = True
                        progressed = True
                if not started_any:
                    break

            if wave:
                waves.append(wave)

            if running:
                running.sort(key=lambda item: (item["finish"], rank[item["id"]]))
                next_finish = running[0]["finish"]
                now = next_finish
                done = [r for r in running if r["finish"] <= now + 1e-9]
                running = [r for r in running if r["finish"] > now + 1e-9]
                for record in done:
                    sid = record["id"]
                    for key, value in self.steps[sid].resources.items():
                        used[key] -= value
                    completed.add(sid)
                    for child_id, step in self.steps.items():
                        if sid in step.requires:
                            indeg[child_id] -= 1
                progressed = True

            if not progressed:
                raise PlanError("调度停滞：资源不足导致无法继续（不应发生）")

        makespan = max((entry["finish"] for entry in timeline), default=0.0)
        timeline.sort(key=lambda e: (e["start"], e["wave"],
                                     rank[e["id"]]))
        return Plan(order=order, waves=waves, timeline=timeline,
                    estimated_makespan=round(makespan, 6))

    # ---- 回滚计划：已执行步骤按完成顺序的逆序 ----
    @staticmethod
    def rollback_order(completion_order: List[str]) -> List[str]:
        return list(reversed(completion_order))
