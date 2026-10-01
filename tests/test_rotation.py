"""证书轮换编排自测（标准库 unittest，可注入时钟，完全确定性）。

覆盖：
1. 正常轮换：新旧并存 -> 按比例切换 -> 下线旧证书，输出时间线。
2. 证书即将过期时轮换。
3. 证书已过期后的紧急轮换。
4. 轮换失败（部署中途失败）-> 回滚 -> 旧证书恢复且已建立连接不受影响。
5. 轮换中断后重启：从任意事件点崩溃都能继续，无“双不生效”窗口。
6. 回滚中断后重启：安全完成回滚，旧证书始终生效。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cert_rotation import (  # noqa: E402
    COMPLETE, IDLE, RETIRED, ROLLING_BACK, SHIFTING,
    Certificate, Clock, Gateway, Journal, Orchestrator, RotationError,
)

DAY = 86400.0
CLIENTS = [f"client-{i}" for i in range(64)]


def make_env(tmpdir, start=1_000_000.0, **orch_kwargs):
    clock = Clock(start)
    gateway = Gateway(clock)
    journal = Journal(os.path.join(tmpdir, "rotation-state.json"))
    orch = Orchestrator(gateway, journal, clock, **orch_kwargs)
    return clock, gateway, journal, orch


def make_certs(clock, old_ttl=30 * DAY, new_ttl=365 * DAY):
    now = clock.now()
    old = Certificate("cert-old", now - 300 * DAY, now + old_ttl)
    new = Certificate("cert-new", now, now + new_ttl)
    return old, new



def install_old(gateway, old):
    gateway.install(old)
    gateway.set_split(100, old.serial, old.serial)

def fresh_handshakes(gateway):
    return gateway.handshakes(CLIENTS)


def assert_all_serving(testcase, gateway):
    ok, failed = fresh_handshakes(gateway)
    testcase.assertEqual(failed, [], f"handshake failures: {failed}")
    return ok


def assert_connections_alive(testcase, conns):
    for conn in conns:
        testcase.assertTrue(conn.serve())


def drive_to_completion(clock, orch, seconds_per_step=3600.0, max_steps=100):
    for _ in range(max_steps):
        state = orch.pump()
        if state == RETIRED:
            return
        clock.advance(seconds_per_step)
    raise AssertionError(f"rotation did not finish, state={orch.state}")


class NormalRotationTest(unittest.TestCase):
    """情形：正常轮换（含即将过期），新旧并存、按比例切换、下线旧证书。"""

    def test_gradual_rotation_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, gw, _, orch = make_env(tmp)
            old, new = make_certs(clock, old_ttl=7 * DAY)  # 即将过期
            install_old(gw, old)

            # 轮换前：全部走旧证书
            before, failed = fresh_handshakes(gw)
            self.assertEqual(failed, [])
            self.assertTrue(all(c.cert_serial == old.serial for c in before))

            orch.start(old, new, shift_plan=[(3600, 75), (7200, 50), (10800, 25), (14400, 0)])
            self.assertEqual(orch.state, "PREPARED")
            # 新旧并存
            self.assertEqual(set(gw.installed), {old.serial, new.serial})
            assert_all_serving(self, gw)

            seen_splits = []
            for _ in range(5):
                clock.advance(3600)
                orch.pump()
                seen_splits.append(gw.pct_old)
                assert_all_serving(self, gw)  # 全程无失败握手
            self.assertEqual(seen_splits, [75, 50, 25, 0, 0])
            self.assertEqual(orch.state, RETIRED)
            # 旧证书已下线，新证书独存
            self.assertEqual(set(gw.installed), {new.serial})
            ok, failed = fresh_handshakes(gw)
            self.assertEqual(failed, [])
            self.assertTrue(all(c.cert_serial == new.serial for c in ok))
            # 轮换前建立的连接仍然可用
            assert_connections_alive(self, before)

            # 时间线包含关键事件
            kinds = [e["event"] for e in orch.timeline]
            for expected in ("rotation_started", "new_cert_installed",
                             "shifting_began", "traffic_shifted",
                             "shift_complete", "old_cert_retired"):
                self.assertIn(expected, kinds)
            self.assertIn("t+", orch.render_timeline())

    def test_rotation_completes_before_old_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, gw, _, orch = make_env(tmp)
            old, new = make_certs(clock, old_ttl=5 * 3600)  # 5 小时后过期
            install_old(gw, old)
            orch.start(old, new, shift_plan=[(600, 50), (1200, 0)])
            drive_to_completion(clock, orch, seconds_per_step=600)
            self.assertEqual(orch.state, RETIRED)
            self.assertLess(clock.now(), old.not_after)  # 旧证书过期前完成
            assert_all_serving(self, gw)


class ExpiredOldCertTest(unittest.TestCase):
    """情形：旧证书已过期才发起轮换 -> 紧急切换。"""

    def test_emergency_rotation_after_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, gw, _, orch = make_env(tmp)
            old, new = make_certs(clock, old_ttl=3600)
            install_old(gw, old)
            clock.advance(7200)  # 旧证书已过期

            # 过期期间新握手失败（这是发起轮换前的既成事实）
            _, failed = fresh_handshakes(gw)
            self.assertEqual(len(failed), len(CLIENTS))

            # 非紧急模式拒绝启动
            with self.assertRaises(RotationError):
                orch.start(old, new, shift_plan=[(0, 0)])

            orch.start(old, new, shift_plan=[(3600, 0)], emergency=True)
            orch.pump()  # 紧急：立即全量切换
            orch.pump()  # 下线旧证书
            self.assertEqual(orch.state, RETIRED)
            self.assertEqual(set(gw.installed), {new.serial})
            ok, failed = fresh_handshakes(gw)
            self.assertEqual(failed, [])
            self.assertTrue(all(c.cert_serial == new.serial for c in ok))

    def test_invalid_new_cert_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, gw, _, orch = make_env(tmp)
            old, new = make_certs(clock)
            install_old(gw, old)
            bad = Certificate("cert-bad", clock.now() - 10 * DAY, clock.now() - DAY)
            with self.assertRaises(RotationError):
                orch.start(old, bad, shift_plan=[(0, 0)])
            self.assertEqual(orch.state, IDLE)  # 状态未污染
            assert_all_serving(self, gw)


class RollbackTest(unittest.TestCase):
    """情形：轮换失败 -> 回滚；验证旧证书恢复 + 已建立连接不受影响。"""

    def test_deploy_failure_then_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, gw, _, orch = make_env(tmp, deploy_fail_at=50)
            old, new = make_certs(clock)
            install_old(gw, old)
            conns_old, _ = fresh_handshakes(gw)

            orch.start(old, new, shift_plan=[(600, 75), (1200, 50), (1800, 0)])
            clock.advance(600)
            orch.pump()  # -> 75% ok
            conns_mixed, failed = fresh_handshakes(gw)
            self.assertEqual(failed, [])
            self.assertTrue(any(c.cert_serial == new.serial for c in conns_mixed))

            clock.advance(600)
            with self.assertRaises(RotationError):
                orch.pump()  # 切到 50% 时部署失败
            self.assertEqual(orch.state, SHIFTING)  # 停在安全中间态
            assert_all_serving(self, gw)  # 仍无失败握手

            orch.rollback(reason="deploy failure at 50%")
            self.assertEqual(orch.state, IDLE)
            # 旧证书重新 100% 生效，新证书下线
            self.assertEqual(gw.pct_old, 100)
            self.assertEqual(set(gw.installed), {old.serial})
            after, failed = fresh_handshakes(gw)
            self.assertEqual(failed, [])
            self.assertTrue(all(c.cert_serial == old.serial for c in after))
            # 已建立连接（含曾走新证书的）全部不受影响
            assert_connections_alive(self, conns_old + conns_mixed)

    def test_rollback_from_complete_state(self):
        """流量已 100% 到新证书、旧证书尚未下线（COMPLETE）时回滚。"""
        with tempfile.TemporaryDirectory() as tmp:
            clock, gw, _, orch = make_env(tmp)
            old, new = make_certs(clock)
            install_old(gw, old)
            orch.start(old, new, shift_plan=[(600, 0)])
            clock.advance(600)
            orch.pump()  # SHIFTING -> COMPLETE（retire 需再一次 pump）
            self.assertEqual(orch.state, COMPLETE)
            conns_new, failed = fresh_handshakes(gw)
            self.assertEqual(failed, [])
            self.assertTrue(all(c.cert_serial == new.serial for c in conns_new))

            orch.rollback(reason="post-cutover regression")
            self.assertEqual(orch.state, IDLE)
            self.assertEqual(set(gw.installed), {old.serial})
            after, failed = fresh_handshakes(gw)
            self.assertEqual(failed, [])
            self.assertTrue(all(c.cert_serial == old.serial for c in after))
            assert_connections_alive(self, conns_new)  # 新证书上的连接不受影响


class CrashRecoveryTest(unittest.TestCase):
    """情形：轮换中断后重启可继续；任何事件点崩溃都无“双不生效”窗口。"""

    CRASH_EVENTS = ["prepared", "shift_applied", "complete", "retired"]

    def _run_crash_at(self, crash_event, tmp):
        clock = Clock()
        gw = Gateway(clock)
        old, new = make_certs(clock)
        gw.install(old)
        conns_before, _ = gw.handshakes(CLIENTS)
        journal = Journal(os.path.join(tmp, "state.json"))

        crashed = {"done": False}

        def hook(event):
            if event == crash_event and not crashed["done"]:
                crashed["done"] = True
                raise RuntimeError("simulated crash")

        orch = Orchestrator(gw, journal, clock, on_event=hook)
        try:
            orch.start(old, new, shift_plan=[(600, 50), (1200, 0)])
            drive_to_completion(clock, orch, seconds_per_step=600)
        except RuntimeError:
            pass
        self.assertTrue(crashed["done"], f"crash at {crash_event} never fired")

        # 崩溃瞬间：至少一套证书在生效（无双不生效窗口）
        old_up, new_up = gw.serving_certificates()
        self.assertTrue(old_up or new_up)

        # 重启：新 Orchestrator 加载同一日志并对同一网关对账，随后继续
        orch2 = Orchestrator(gw, journal, clock)
        drive_to_completion(clock, orch2, seconds_per_step=600)
        self.assertEqual(orch2.state, RETIRED)
        self.assertEqual(set(gw.installed), {new.serial})
        ok, failed = gw.handshakes(CLIENTS)
        self.assertEqual(failed, [])
        self.assertTrue(all(c.cert_serial == new.serial for c in ok))
        # 崩溃前建立的连接仍然可用
        for conn in conns_before:
            self.assertTrue(conn.serve())
        return orch2

    def test_crash_at_every_event_point(self):
        for event in self.CRASH_EVENTS:
            with self.subTest(crash_at=event), tempfile.TemporaryDirectory() as tmp:
                self._run_crash_at(event, tmp)

    def test_no_dual_outage_window_across_whole_rotation(self):
        """细粒度扫描：每个事件点崩溃，崩溃前后都不允许两套同时失效。"""
        with tempfile.TemporaryDirectory() as tmp:
            for event in self.CRASH_EVENTS:
                sub = os.path.join(tmp, event)
                os.makedirs(sub)
                clock = Clock()
                gw = Gateway(clock)
                old, new = make_certs(clock)
                install_old(gw, old)
                journal = Journal(os.path.join(sub, "state.json"))
                fired = {"done": False}

                def hook(ev, _event=event):
                    if ev == _event and not fired["done"]:
                        fired["done"] = True
                        raise RuntimeError("crash")

                orch = Orchestrator(gw, journal, clock, on_event=hook)
                try:
                    orch.start(old, new, shift_plan=[(600, 50), (1200, 0)])
                    drive_to_completion(clock, orch, seconds_per_step=600)
                except RuntimeError:
                    pass
                old_up, new_up = gw.serving_certificates()
                self.assertTrue(old_up or new_up, f"dual outage after crash at {event}")
                orch2 = Orchestrator(gw, journal, clock)
                old_up, new_up = gw.serving_certificates()
                self.assertTrue(old_up or new_up, f"dual outage after reconcile at {event}")
                drive_to_completion(clock, orch2, seconds_per_step=600)


class RollbackCrashTest(unittest.TestCase):
    """情形：回滚中断 -> 重启后安全完成回滚，旧证书始终生效。"""

    def test_crash_during_rollback_then_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            gw = Gateway(clock)
            old, new = make_certs(clock)
            install_old(gw, old)
            conns_before, _ = gw.handshakes(CLIENTS)
            journal = Journal(os.path.join(tmp, "state.json"))

            orch = Orchestrator(gw, journal, clock)
            orch.start(old, new, shift_plan=[(600, 50), (1200, 0)])
            clock.advance(600)
            orch.pump()
            conns_mixed, failed = gw.handshakes(CLIENTS)
            self.assertEqual(failed, [])

            fired = {"done": False}

            def hook(event):
                if event == "rollback_started" and not fired["done"]:
                    fired["done"] = True
                    raise RuntimeError("crash during rollback")

            orch.on_event = hook
            with self.assertRaises(RuntimeError):
                orch.rollback(reason="regression")
            self.assertTrue(fired["done"])

            # 崩溃后：旧证书仍安装且承载流量（回滚 WAL 已记录）
            old_up, _ = gw.serving_certificates()
            self.assertTrue(old_up)
            ok, failed = gw.handshakes(CLIENTS)
            self.assertEqual(failed, [])

            # 重启：对账后幂等完成回滚
            orch2 = Orchestrator(gw, journal, clock)
            self.assertEqual(orch2.state, ROLLING_BACK)
            orch2.pump()
            self.assertEqual(orch2.state, IDLE)
            self.assertEqual(gw.pct_old, 100)
            self.assertEqual(set(gw.installed), {old.serial})
            after, failed = gw.handshakes(CLIENTS)
            self.assertEqual(failed, [])
            self.assertTrue(all(c.cert_serial == old.serial for c in after))
            # 全部历史连接（含曾走新证书的）不受影响
            for conn in conns_before + conns_mixed:
                self.assertTrue(conn.serve())

    def test_crash_after_rollback_done_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            gw = Gateway(clock)
            old, new = make_certs(clock)
            install_old(gw, old)
            journal = Journal(os.path.join(tmp, "state.json"))
            orch = Orchestrator(gw, journal, clock)
            orch.start(old, new, shift_plan=[(600, 0)])
            clock.advance(600)
            orch.pump()
            orch.rollback(reason="done-then-crash")
            # 回滚完成后重启：状态保持 IDLE，旧证书生效
            orch2 = Orchestrator(gw, journal, clock)
            self.assertEqual(orch2.state, IDLE)
            ok, failed = gw.handshakes(CLIENTS)
            self.assertEqual(failed, [])
            self.assertTrue(all(c.cert_serial == old.serial for c in ok))


if __name__ == "__main__":
    unittest.main(verbosity=2)
