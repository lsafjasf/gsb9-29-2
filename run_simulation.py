"""Long-run simulation producing residual-over-time and filter statistics.

Scenario (1 h, 1 exchange/s):
  * asymmetric RTT: 30 ms forward / 10 ms backward, +-2 ms jitter
  * 1% of exchanges get a +100 ms network spike
  * local clock: 200 ms initial offset, 40 ppm drift
  * +400 ms local-clock step at t = 1800 s
  * link outage from t = 2700 s to t = 2760 s

Usage:  python3 run_simulation.py [residuals.csv]
"""

import csv
import sys

from simulation import LocalClock, Network, Scenario, run
from timesync import Synchronizer
from timesync_naive import NaiveSynchronizer

T_END = 3600.0
REPORT_EVERY = 300.0


def main():
    out_csv = sys.argv[1] if len(sys.argv) > 1 else "residuals.csv"

    clock = LocalClock(offset=0.2, skew=40e-6)
    clock.add_step(1800.0, 0.4)
    net = Network(seed=42, fwd=0.03, bwd=0.01, jitter=2e-3,
                  spike_prob=0.01, spike_mag=0.1,
                  outages=[(2700.0, 2760.0)])
    scenario = Scenario(clock, net)

    naive = NaiveSynchronizer()
    fixed = Synchronizer(holdover_after=30.0)
    rows = []

    def on_row(t, sync, scn):
        est_n = naive.offset_estimate()
        est_f = fixed.offset_estimate()
        true = scn.true_offset(t)
        rows.append({
            "t": t,
            "true_offset_ms": true * 1e3,
            "naive_residual_ms": None if est_n is None else (est_n - true) * 1e3,
            "fixed_residual_ms": None if est_f is None else (est_f - true) * 1e3,
            "status": fixed.status(scn.clock.read(t)),
        })

    # Drive naive and fixed on identical streams by replaying the scenario
    # (same seed -> identical delay/jitter/spike sequence).
    run(scenario, naive, T_END, on_row=None)
    clock2 = LocalClock(offset=0.2, skew=40e-6)
    clock2.add_step(1800.0, 0.4)
    net2 = Network(seed=42, fwd=0.03, bwd=0.01, jitter=2e-3,
                   spike_prob=0.01, spike_mag=0.1,
                   outages=[(2700.0, 2760.0)])
    scenario = Scenario(clock2, net2)
    run(scenario, fixed, T_END, on_row=on_row)

    print("filter criterion:", Synchronizer.FILTER_CRITERION)
    print()
    hdr = ("t[s]", "true_off[ms]", "naive_res[ms]", "fixed_res[ms]", "status")
    print("%6s %13s %14s %14s %s" % hdr)
    for row in rows:
        if row["t"] % REPORT_EVERY == 0 or row["t"] in (1799.0, 1801.0,
                                                        1805.0, 2759.0):
            print("%6.0f %13.3f %14s %14s %s" % (
                row["t"],
                row["true_offset_ms"],
                "-" if row["naive_residual_ms"] is None
                else "%.3f" % row["naive_residual_ms"],
                "-" if row["fixed_residual_ms"] is None
                else "%.3f" % row["fixed_residual_ms"],
                row["status"],
            ))

    stats = fixed.stats()
    print()
    print("fixed synchronizer summary")
    print("  samples total/filtered : %d / %d (filter ratio %.2f%%)"
          % (stats["total"], stats["filtered"],
             100.0 * stats["filter_ratio"]))
    print("  failed polls (outage)  : %d" % stats["failed_polls"])
    print("  estimated skew         : %.2f ppm (true -40.00 ppm)"
          % (stats["skew"] * 1e6))
    print("  residual sigma         : %.3f ms"
          % (stats["residual_sigma"] * 1e3))
    print("  step events            : %d" % len(fixed.step_events))
    for ev in fixed.step_events:
        print("    t=%.0fs magnitude=%.3f ms" % (ev.t, ev.magnitude * 1e3))

    tail = [r for r in rows if r["t"] > 3000.0
            and r["fixed_residual_ms"] is not None]
    worst = max(abs(r["fixed_residual_ms"]) for r in tail)
    print("  worst |residual| after recovery (t>3000s): %.3f ms" % worst)

    with open(out_csv, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print()
    print("full residual series written to %s (%d rows)" % (out_csv, len(rows)))


if __name__ == "__main__":
    main()
