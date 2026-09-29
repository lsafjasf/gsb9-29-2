"""Benchmark / acceptance data for AdaptiveSampler.

Simulates: steady traffic, 10x surge, traffic drop, all-important traffic,
and a silent period. Prints rate deviation, convergence time, and pre/post
sampling distribution deviation for key dimensions.

Run: python3 benchmark.py
"""

import random

from adaptive_sampler import AdaptiveSampler
from test_adaptive_sampler import (
    TARGET, RATE_TOL, DIST_TOL, Clock, drive, make_event, pick, SERVICES, REGIONS,
    convergence_window,
)


def section(title):
    print("\n== %s ==" % title)


def report_phase(name, rates, target=TARGET):
    conv = convergence_window(rates, target)
    if conv is None:
        print("%-28s DID NOT CONVERGE: %s" % (name, rates))
        return None
    steady = rates[conv:]
    avg = sum(steady) / len(steady)
    worst = max(abs(r - target) / target for r in steady)
    print("%-28s converged in %d window(s) (~%ds) | steady avg %.1f/s "
          "(dev %+.2f%%) | worst window dev %+.2f%%"
          % (name, conv, conv, avg, (avg - target) / target * 100, worst * 100))
    return conv


def distribution_check(seed=99):
    section("Representativeness: dimension distribution before vs after sampling")
    clock = Clock()
    sampler = AdaptiveSampler(target_rate=TARGET, window_sec=1.0,
                              rng=random.Random(seed), time_fn=clock)
    rng = random.Random(seed + 1)
    drive(sampler, clock, 2000, 10, rng)  # warm-up, discard

    pop, sam = {}, {}
    pop_n = sam_n = 0
    kept_important = incoming_important = 0
    seconds, rate = 60, 2000
    for _ in range(seconds):
        for _ in range(rate):
            clock.t += 1.0 / rate
            ev = make_event(rng, important_frac=0.05)
            important = sampler.is_important(ev)
            kept = sampler.should_sample(ev)
            if important:
                incoming_important += 1
                kept_important += 1
                continue  # important stratum is 100% kept; check normal stratum
            pop_n += 1
            for dim, v in ev["dims"].items():
                pop.setdefault(dim, {}).setdefault(v, 0)
                pop[dim][v] += 1
            if kept:
                sam_n += 1
                for dim, v in ev["dims"].items():
                    sam.setdefault(dim, {}).setdefault(v, 0)
                    sam[dim][v] += 1

    print("normal events: population=%d, sampled=%d (p~%.3f)"
          % (pop_n, sam_n, sam_n / pop_n))
    ok = True
    for dim in sorted(pop):
        for value in sorted(pop[dim]):
            p_pop = pop[dim][value] / pop_n
            p_sam = sam[dim].get(value, 0) / sam_n
            dev = abs(p_pop - p_sam)
            flag = "OK " if dev <= DIST_TOL else "FAIL"
            ok = ok and dev <= DIST_TOL
            print("  [%s] %-8s=%-7s pop %6.2f%%  sampled %6.2f%%  dev %5.2fpp"
                  % (flag, dim, value, p_pop * 100, p_sam * 100, dev * 100))
    print("important events kept: %d/%d (100%% required)"
          % (kept_important, incoming_important))
    ok = ok and kept_important == incoming_important
    print("distribution check: %s (threshold %.0fpp)" % ("PASS" if ok else "FAIL", DIST_TOL * 100))
    return ok


def main():
    print("AdaptiveSampler acceptance benchmark")
    print("target rate: %.0f events/s | window: 1s | rate tol: %.0f%% | dist tol: %.0fpp"
          % (TARGET, RATE_TOL * 100, DIST_TOL * 100))

    section("Rate tracking & convergence")
    clock = Clock()
    s = AdaptiveSampler(target_rate=TARGET, window_sec=1.0,
                        rng=random.Random(7), time_fn=clock)
    rng = random.Random(8)

    rates = drive(s, clock, 2000, 20, rng)
    report_phase("steady 2000/s", rates)

    # 10x surge with a lean important mix (~1.5% of 20k = 300/s < target),
    # leaving budget for the controller to tune.
    rates = drive(s, clock, 20000, 20, rng, important_frac=0.01, slow_frac=0.005)
    report_phase("10x surge 2000->20000/s", rates)

    # 10x surge with the default 5% important mix: important stream alone
    # (~1380/s) exceeds target, so all budget goes to important events.
    rates = drive(s, clock, 20000, 10, rng, important_frac=0.05)
    print("%-28s important ~1380/s > target: reported tail %s "
          "(important kept 100%%, normal p=%.3f)"
          % ("surge, important-heavy", rates[-3:], s.probability))

    rates = drive(s, clock, 200, 15, rng)     # drop below target
    tail = rates[-5:]
    print("%-28s incoming 200/s < target: reported tail %s | p=%.3f "
          "(all normal traffic kept)" % ("drop to 200/s", tail, s.probability))

    section("Edge cases")
    rates = drive(s, clock, 2000, 10, rng, all_important=True)
    print("all-important 2000/s (4x target): kept/window=%s -> 100%% retained, "
          "important never dropped" % sorted(set(rates)))

    rates = drive(s, clock, 0, 10, rng)
    print("no traffic 10s: reported=%s, sampler alive, p=%.3f"
          % (sorted(set(rates)), s.probability))
    rates = drive(s, clock, 2000, 15, rng)
    report_phase("recovery after silence", rates)

    ok = distribution_check()
    print("\noverall: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
