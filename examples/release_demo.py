"""发布编排演示：成功发布、失败回滚、断点续跑三个场景。

运行: python3 examples/release_demo.py
"""

import os
import tempfile
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import (
    Executor,
    Step,
    World,
    build_plan,
    build_report,
    render_report,
)


def show_plan(plan, title):
    print(f"===== {title} =====")
    for wave in plan.waves:
        print(f"  波次 {wave.index}  [{wave.start:g}~{wave.end:g}]  并行: {wave.steps}")
    print(f"  总工期: {plan.makespan:g}")
    print(f"  执行顺序: {plan.order}")
    print(f"  回滚顺序: {plan.rollback_order}")
    print()


def main():
    tmpdir = tempfile.mkdtemp(prefix="release-demo-")

    steps = {
        "db-migrate": Step("db-migrate", duration=2.0, resources={"cpu": 2, "mem": 4}),
        "svc-a": Step("svc-a", deps=("db-migrate",), duration=3.0,
                      resources={"cpu": 3, "mem": 6}, fail_times=1, max_retries=2),
        "svc-b": Step("svc-b", deps=("db-migrate",), duration=2.0,
                      resources={"cpu": 3, "mem": 6}, actual_duration=5.0),
        "frontend": Step("frontend", deps=("svc-a", "svc-b"), duration=1.0,
                         resources={"cpu": 2, "mem": 2}),
        "cache-warm": Step("cache-warm", deps=("frontend",), duration=1.0,
                           resources={"cpu": 1, "mem": 1}),
    }
    capacities = {"cpu": 8, "mem": 16}
    plan = build_plan(steps, capacities)
    show_plan(plan, "执行计划与回滚计划（资源上限 cpu=8 mem=16）")

    # 场景 1：成功发布，svc-a 重试一次，svc-b 耗时异常
    print("===== 场景 1：成功发布 =====")
    world = World()
    state_path = os.path.join(tmpdir, "ok.json")
    result = Executor(plan, steps, world, state_path, retry_delay=0.5).run()
    print(f"状态={result.status}, 执行={result.executed}")
    print(render_report(build_report(plan, result)))
    print()

    # 场景 2：frontend 必然失败 -> 已执行步骤逆序回滚，cache-warm 被跳过
    print("===== 场景 2：失败回滚 =====")
    failing = dict(steps)
    failing["frontend"] = Step("frontend", deps=("svc-a", "svc-b"),
                               resources={"cpu": 2, "mem": 2},
                               always_fail=True, max_retries=1)
    plan2 = build_plan(failing, capacities)
    result2 = Executor(plan2, failing, World(),
                       os.path.join(tmpdir, "fail.json"),
                       retry_delay=0.5).run()
    print(f"状态={result2.status}")
    # 回滚断言：失败步骤半成品先补偿，之后严格为已执行步骤的逆序
    assert result2.rolled_back[0] == "frontend"
    assert result2.rolled_back[1:] == list(reversed(result2.executed))
    print(render_report(build_report(plan2, result2)))
    print()

    # 场景 3：完成 2 步后中断，从检查点续跑，结果与场景 1 一致
    print("===== 场景 3：断点续跑 =====")
    world3 = World()
    path3 = os.path.join(tmpdir, "resume.json")
    first = Executor(plan, steps, world3, path3, retry_delay=0.5).run(pause_after=2)
    print(f"中断时: 状态={first.status}, 已完成={first.executed}")
    resumed = Executor(plan, steps, world3, path3, retry_delay=0.5).run()
    print(f"续跑后: 状态={resumed.status}, 执行={resumed.executed}")
    assert resumed.executed == result.executed
    assert world3.applied == world.applied
    print("断言通过：续跑结果与一次跑完完全一致")


if __name__ == "__main__":
    main()
