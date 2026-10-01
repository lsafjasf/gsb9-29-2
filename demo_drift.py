"""漂移自适应演示：打印客户端时钟偏移 -> 估计 -> 窗口调整的数据。

运行：python3 demo_drift.py
"""
from drift import DriftEstimator
from replay import ReplayStore
from totp import TOTP
from verifier import Verifier

KEY = b"12345678901234567890"
T_START = 1_700_000_000


def run(skew_seconds: int):
    t = float(T_START)
    totp = TOTP(KEY, digits=6, period=30, clock=lambda: t)
    drift = DriftEstimator(max_samples=16, min_samples=3)
    v = Verifier(totp, ReplayStore(), drift=drift,
                 back=1, forward=1,
                 learn_back=4, learn_forward=4, user="dev")
    print("\n客户端时钟偏移 %+d 秒（period=30）" % skew_seconds)
    print("  次序 | 服务器时刻偏移 | 实测offset | 估计est | 阶段     | 候选窗口(相对)")
    for i in range(8):
        cur = totp.counter_at(t)
        lo0, hi0 = v._candidate_range(cur)
        code = totp.at(t + skew_seconds)
        ok = v.verify(code)
        assert ok
        stage = "学习期" if drift.is_learning("dev") else "已收敛"
        print("  %3d  | %12ds | %+9d | %+6d | %s | [%+d, %+d]"
              % (i + 1, t - T_START, drift.samples("dev")[-1],
                 drift.estimate("dev"), stage, lo0 - cur, hi0 - cur))
        t += 30


if __name__ == "__main__":
    print("漂移估计与窗口调整演示（成功后容忍窗口中心跟随 est 平移）")
    for skew in (-60, 47):
        run(skew)
