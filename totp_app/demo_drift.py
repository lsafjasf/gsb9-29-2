"""Drift-adaptation demo: prints the estimator's evolving state and window.

Simulates a client whose clock runs 2 windows (60 s) ahead of the server.
The verifier starts with a permissive bootstrap window, then tightens around
the learned offset as evidence accumulates.

Run: python3 demo_drift.py
"""

from totp import DriftEstimator, Verifier, totp

SECRET = b"12345678901234567890"
STEP = 30
SKEW_WINDOWS = 2


def main() -> None:
    server_now = [1_000_000.0]
    v = Verifier(
        time_func=lambda: server_now[0],
        drift_kwargs=dict(base_spread=1, bootstrap_spread=4,
                          bootstrap_samples=5, alpha=0.5),
    )
    est = v._estimator("alice")

    print("client skew: +%d windows (%+d s)" % (SKEW_WINDOWS,
                                                SKEW_WINDOWS * STEP))
    print("%-7s %-8s %-9s %-7s %-13s" %
          ("sample", "offset", "ema", "std", "window[lo,hi]"))
    print("-" * 50)

    for i in range(1, 9):
        client_time = server_now[0] + SKEW_WINDOWS * STEP
        code = totp(SECRET, for_time=client_time, step=STEP)
        res = v.verify("alice", SECRET, code)
        lo, hi = est.window()
        print("%-7d %-+8d %-9.3f %-7.3f [%+d, %+d]  %s" %
              (i, res.matched_offset, est.ema, est.std, lo, hi,
               "bootstrap" if est.samples <= 5 else "adapted"))
        server_now[0] += STEP

    lo, hi = est.window()
    print()
    print("after learning: accepted offsets [%+d, %+d] -> %d-window scan"
          % (lo, hi, hi - lo + 1))
    print("a code at offset -4 (accepted during bootstrap) is now rejected:")
    far = totp(SECRET, for_time=server_now[0] - 4 * STEP, step=STEP)
    print("  verify ->", bool(v.verify("alice", SECRET, far)))


if __name__ == "__main__":
    main()
