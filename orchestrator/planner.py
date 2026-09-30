"""规划器：解析依赖与资源占用，输出满足资源上限的执行计划与回滚计划。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from .model import Step


class PlanningError(Exception):
    """规划阶段错误基类。"""


class CycleError(PlanningError):
    def __init__(self, path: List[str]) -> None:
        self.path = list(path)  # 完整环路径，首尾相同
        super().__init__("检测到循环依赖: " + " -> ".join(self.path))


class UnknownDependencyError(PlanningError):
    def __init__(self, step_name: str, dep: str) -> None:
        self.step_name = step_name
        self.dep = dep
        super().__init__(f"步骤 {step_name!r} 依赖了不存在的步骤 {dep!r}")


class UnknownResourceError(PlanningError):
    def __init__(self, step_name: str, resource: str) -> None:
        self.step_name = step_name
        self.resource = resource
        super().__init__(f"步骤 {step_name!r} 使用了未声明上限的资源 {resource!r}")


class ResourceOverflowError(PlanningError):
    def __init__(self, step_name: str, resource: str, need: float, cap: float) -> None:
        self.step_name = step_name
        self.resource = resource
        self.need = need
        self.cap = cap
        super().__init__(
            f"步骤 {step_name!r} 需要 {resource}={need}，超过资源上限 {cap}"
        )


@dataclass(frozen=True)
class Wave:
    index: int
    steps: List[str]
    start: float
    end: float


@dataclass(frozen=True)
class Plan:
    order: List[str]          # 拓扑顺序
    waves: List[Wave]         # 资源受限的并行波次
    rollback_order: List[str]  # 回滚顺序 = 拓扑顺序的逆序
    makespan: float


def normalize_steps(steps: Mapping[str, Step] | Iterable[Step]) -> Dict[str, Step]:
    if isinstance(steps, Mapping):
        return dict(steps)
    result: Dict[str, Step] = {}
    for step in steps:
        if step.name in result:
            raise PlanningError(f"重复的步骤名: {step.name!r}")
        result[step.name] = step
    return result


def find_cycle(steps: Mapping[str, Step]) -> Optional[List[str]]:
    """三色 DFS；发现回边时从 DFS 栈切出完整环（首尾相同）。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {name: WHITE for name in steps}
    stack: List[str] = []

    def dfs(u: str) -> Optional[List[str]]:
        color[u] = GRAY
        stack.append(u)
        for v in steps[u].deps:
            if v not in steps:
                continue  # 未知依赖由调用方校验
            if color[v] == GRAY:
                i = stack.index(v)
                return stack[i:] + [v]
            if color[v] == WHITE:
                cycle = dfs(v)
                if cycle is not None:
                    return cycle
        stack.pop()
        color[u] = BLACK
        return None

    for name in sorted(steps):
        if color[name] == WHITE:
            cycle = dfs(name)
            if cycle is not None:
                return cycle
    return None


def topological_order(steps: Mapping[str, Step]) -> List[str]:
    """Kahn 算法；同层按名称排序，保证计划确定可复现。"""
    indeg = {name: len(steps[name].deps) for name in steps}
    dependents: Dict[str, List[str]] = {name: [] for name in steps}
    for name, step in steps.items():
        for dep in step.deps:
            dependents[dep].append(name)

    ready = sorted(n for n, d in indeg.items() if d == 0)
    order: List[str] = []
    while ready:
        u = ready.pop(0)
        order.append(u)
        for v in dependents[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                ready.append(v)
                ready.sort()
    if len(order) != len(steps):
        raise CycleError(find_cycle(steps) or [])
    return order


def build_plan(
    steps: Mapping[str, Step] | Iterable[Step],
    capacities: Dict[str, float],
) -> Plan:
    """构建执行计划。

    - 校验未知依赖、未声明资源、单步需求超过资源上限；
    - 有环时抛 CycleError，携带完整环路径；
    - 采用表调度：每一波取所有依赖已完成、且资源能放下的就绪步骤，
      并行波次不超过任一资源上限；
    - 回滚计划为拓扑顺序的严格逆序（先回滚依赖方，再回滚被依赖方）。
    """
    sm = normalize_steps(steps)

    for step in sm.values():
        for dep in step.deps:
            if dep not in sm:
                raise UnknownDependencyError(step.name, dep)
        for resource, need in step.resources.items():
            if resource not in capacities:
                raise UnknownResourceError(step.name, resource)
            if need > capacities[resource]:
                raise ResourceOverflowError(step.name, resource, need, capacities[resource])

    cycle = find_cycle(sm)
    if cycle is not None:
        raise CycleError(cycle)

    order = topological_order(sm)

    waves: List[Wave] = []
    done = set()
    remaining = set(sm)
    clock = 0.0
    while remaining:
        ready = sorted(
            n for n in remaining if all(d in done for d in sm[n].deps)
        )
        used: Dict[str, float] = {r: 0.0 for r in capacities}
        picked: List[str] = []
        for name in ready:
            fit = all(
                used[r] + sm[name].resources.get(r, 0) <= cap
                for r, cap in capacities.items()
            )
            if fit:
                picked.append(name)
                for r, amount in sm[name].resources.items():
                    used[r] += amount
        if not picked:
            # 理论不可达：每个单步都已校验不超过上限，自己单独即可成波
            raise PlanningError("内部错误：无法调度任何就绪步骤")
        wave_end = clock + max(sm[n].duration for n in picked)
        waves.append(Wave(len(waves), picked, clock, wave_end))
        clock = wave_end
        done.update(picked)
        remaining.difference_update(picked)

    return Plan(
        order=order,
        waves=waves,
        rollback_order=list(reversed(order)),
        makespan=clock,
    )
