"""证书轮换编排演示：时间线、回滚验证、中断恢复。

运行：python3 demo.py
"""

import os
import tempfile

from cert_rotation import Certificate, Clock, Gateway, Journal, Orchestrator, RotationError

DAY = 86400.0
CLIENTS = [f"client-{i}" for i in range(16)]


def banner(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def pct_bar(pct_old):
    old_blocks = pct_old // 10
    return "OLD " + "#" * old_blocks + "-" * (10 - old_blocks) + f" NEW  ({pct_old}%/{100 - pct_old}%)"


def scenario_normal(tmp):
    banner("场景 1：证书即将过期 -> 正常按比例轮换（新旧并存 -> 下线旧证书）")
    clock = Clock()
    gw = Gateway(clock)
    old = Certificate("cert-old", clock.now() - 350 * DAY, clock.now() + 5 * DAY)
    new = Certificate("cert-new", clock.now(), clock.now() + 365 * DAY)
    gw.install(old)
    gw.set_split(100, old.serial, old.serial)
    orch = Orchestrator(gw, Journal(os.path.join(tmp, "s1.json")), clock)

    print(f"旧证书剩余有效期: {old.remaining(clock.now()) / DAY:.1f} 天")
    orch.start(old, new, shift_plan=[(3600, 75), (7200, 50), (10800, 25), (14400, 0)])
    for _ in range(6):
        clock.advance(3600)
        orch.pump()
        ok, failed = gw.handshakes(CLIENTS)
        n_new = sum(1 for c in ok if c.cert_serial == new.serial)
        print(f"  {pct_bar(gw.pct_old)}  新握手 {len(ok)} 成功 / {len(failed)} 失败"
              f"（其中走新证书 {n_new}）")
        if orch.state == "RETIRED":
            break
    print("\n轮换时间线：")
    print(orch.render_timeline())
    print(f"\n终态: state={orch.state}, 已安装证书={sorted(gw.installed)}")


def scenario_rollback(tmp):
    banner("场景 2：轮换中途部署失败 -> 回滚（旧证书恢复，已建立连接不受影响）")
    clock = Clock()
    gw = Gateway(clock)
    old = Certificate("cert-old", clock.now() - 300 * DAY, clock.now() + 30 * DAY)
    new = Certificate("cert-new", clock.now(), clock.now() + 365 * DAY)
    gw.install(old)
    gw.set_split(100, old.serial, old.serial)
    orch = Orchestrator(gw, Journal(os.path.join(tmp, "s2.json")), clock,
                        deploy_fail_at=50)  # 切到 50% 时部署失败

    established, _ = gw.handshakes(CLIENTS[:8])  # 轮换前已建立的连接
    orch.start(old, new, shift_plan=[(600, 75), (1200, 50), (1800, 0)])
    clock.advance(600)
    orch.pump()
    mixed, _ = gw.handshakes(CLIENTS[8:])
    established += mixed
    clock.advance(600)
    try:
        orch.pump()
    except RotationError as exc:
        print(f"  轮换失败: {exc}（state={orch.state}，流量仍 {pct_bar(gw.pct_old)}）")
    orch.rollback(reason="deploy failure at pct_old=50")
    print("\n回滚时间线：")
    print(orch.render_timeline())

    # ---- 回滚验证 ----
    print("\n回滚验证：")
    after, failed = gw.handshakes(CLIENTS)
    assert not failed and all(c.cert_serial == old.serial for c in after)
    print(f"  [OK] 回滚后新握手 100% 走旧证书（{len(after)} 个全部成功）")
    assert set(gw.installed) == {old.serial}
    print(f"  [OK] 新证书已下线，网关仅安装 {sorted(gw.installed)}")
    alive = sum(1 for c in established if c.serve())
    assert alive == len(established)
    print(f"  [OK] 已建立的 {alive} 条连接（含曾走新证书的）全部不受影响")


def scenario_crash_resume(tmp):
    banner("场景 3：轮换中断（崩溃）-> 重启后继续，无“双不生效”窗口")
    clock = Clock()
    gw = Gateway(clock)
    old = Certificate("cert-old", clock.now() - 300 * DAY, clock.now() + 30 * DAY)
    new = Certificate("cert-new", clock.now(), clock.now() + 365 * DAY)
    gw.install(old)
    gw.set_split(100, old.serial, old.serial)
    journal = Journal(os.path.join(tmp, "s3.json"))

    fired = {"done": False}

    def hook(event):
        if event == "shift_applied" and not fired["done"]:
            fired["done"] = True
            raise RuntimeError("process killed")

    orch = Orchestrator(gw, journal, clock, on_event=hook)
    orch.start(old, new, shift_plan=[(600, 50), (1200, 0)])
    clock.advance(600)
    try:
        orch.pump()
    except RuntimeError as exc:
        print(f"  崩溃: {exc}（state={orch.state}，{pct_bar(gw.pct_old)}）")
    old_up, new_up = gw.serving_certificates()
    assert old_up or new_up, "双不生效窗口！"
    print(f"  崩溃瞬间可用性: 旧证书生效={old_up}, 新证书生效={new_up} -> 至少一套生效")

    print("  重启编排器（加载 WAL 日志并对账）...")
    orch2 = Orchestrator(gw, journal, clock)
    clock.advance(600)
    while orch2.pump() != "RETIRED":
        clock.advance(600)
    ok, failed = gw.handshakes(CLIENTS)
    assert not failed and all(c.cert_serial == new.serial for c in ok)
    print(f"  [OK] 续跑完成: state={orch2.state}, 新握手全部走 {new.serial}")
    print("\n恢复后时间线（含崩溃前已落盘的事件由新实例继续追加）：")
    print(orch2.render_timeline())


def scenario_expired(tmp):
    banner("场景 4：旧证书已过期 -> 紧急轮换")
    clock = Clock()
    gw = Gateway(clock)
    old = Certificate("cert-old", clock.now() - 400 * DAY, clock.now() - DAY)  # 已过期
    new = Certificate("cert-new", clock.now(), clock.now() + 365 * DAY)
    gw.install(old)
    gw.set_split(100, old.serial, old.serial)
    _, failed = gw.handshakes(CLIENTS)
    print(f"  过期期间新握手失败 {len(failed)}/{len(CLIENTS)}（轮换前的既成事实）")
    orch = Orchestrator(gw, Journal(os.path.join(tmp, "s4.json")), clock)
    orch.start(old, new, shift_plan=[(3600, 0)], emergency=True)
    orch.pump()
    orch.pump()
    ok, failed = gw.handshakes(CLIENTS)
    assert not failed
    print(f"  [OK] 紧急切换完成: state={orch.state}, 新握手 {len(ok)}/{len(CLIENTS)} 成功")
    print(orch.render_timeline())


def main():
    with tempfile.TemporaryDirectory() as tmp:
        scenario_normal(tmp)
        scenario_rollback(tmp)
        scenario_crash_resume(tmp)
        scenario_expired(tmp)
    print("\n全部演示场景通过。")


if __name__ == "__main__":
    main()
