"""Demo: prints measured bandwidth shares and worst-case wait data.

Run: python3 demo.py
"""

from wfq import WFQSimulator


def backlog(sim, qname, n, size=1.0, t0=0.0):
    for i in range(n):
        sim.submit(qname, size, t0 + i * 1e-9)


def show(title, sim, queues, windows):
    stats = sim.run()
    print("=" * 76)
    print(title)
    print("-" * 76)
    print("%-12s %7s |" % ("queue", "weight") + "".join(
        " %-14s |" % label for label, _, _ in windows)
        + "  maxwait  maxgap")
    for q, w in queues:
        row = "%-12s %7s |" % (q, w)
        for _, t0, t1 in windows:
            row += " %13.1f%% |" % (100 * stats.share(q, t0, t1))
        row += "  %8.2f %8.2f" % (stats.max_wait(q), stats.max_service_gap(q))
        print(row)
    print("  (share = share of bytes in window; maxwait = worst arrival->"
          "service delay;")
    print("   maxgap = worst gap between consecutive services, the "
          "no-starvation metric)")
    print()


def main():
    # 1) Weighted shares, both backlogged
    sim = WFQSimulator(rate=1.0)
    sim.add_queue("bulk", 1)
    sim.add_queue("interactive", 3)
    backlog(sim, "bulk", 4000)
    backlog(sim, "interactive", 4000)
    show("Scenario 1: weights 1:3, both backlogged (expect 25% / 75%)",
         sim, [("bulk", 1), ("interactive", 3)],
         [("[0,4000]", 0.0, 4000.0)])

    # 2) Idle queue: its share is reclaimed immediately
    sim = WFQSimulator(rate=1.0)
    sim.add_queue("a", 1)
    sim.add_queue("b", 1)
    backlog(sim, "b", 1000)
    backlog(sim, "a", 500, t0=500.0)
    show("Scenario 2: A idle until t=500 -> B gets 100%, then 50/50",
         sim, [("a", 1), ("b", 1)],
         [("idle [0,500]", 0.0, 500.0), ("both [510,1000]", 510.0, 1000.0)])

    # 3) Extreme weights 1:100 under full burst
    sim = WFQSimulator(rate=1.0)
    sim.add_queue("thin", 1)
    sim.add_queue("fat", 100)
    backlog(sim, "thin", 50)
    backlog(sim, "fat", 20000)
    show("Scenario 3: weights 1:100, all traffic bursts at t=0 "
         "(thin revisited every ~101 packet times)",
         sim, [("thin", 1), ("fat", 100)],
         [("[0,5000]", 0.0, 5000.0)])

    # 4) Churn: queues join/leave frequently
    sim = WFQSimulator(rate=1.0)
    for name in ("a", "b", "c"):
        sim.add_queue(name, 1)
    for k in range(10):
        backlog(sim, "a", 4, t0=30.0 * k)
        backlog(sim, "b", 4, t0=30.0 * k + 10.0)
    backlog(sim, "c", 500)
    show("Scenario 4: a/b burst 4 pkts in alternating windows, c always "
         "backlogged",
         sim, [("a", 1), ("b", 1), ("c", 1)],
         [("a+c [0,8]", 0.0, 8.0), ("b+c [10,18]", 10.0, 18.0),
          ("only c [20,30]", 20.0, 30.0)])

    # 5) Zero-weight best-effort queue
    sim = WFQSimulator(rate=1.0)
    sim.add_queue("vip", 1)
    sim.add_queue("scavenger", 0)
    backlog(sim, "vip", 100)
    backlog(sim, "scavenger", 100)
    show("Scenario 5: weight 0 = best effort (served only while link idle)",
         sim, [("vip", 1), ("scavenger", 0)],
         [("vip active [0,100]", 0.0, 99.5), ("vip gone (100,200]", 100.5, 200.0)])


if __name__ == "__main__":
    main()
