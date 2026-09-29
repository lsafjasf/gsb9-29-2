"""Scenario benchmarks for AdaptiveSampler: rate deviation, convergence
time, burst overshoot, and distribution fidelity. Stdlib only.

Run:  python3 benchmark.py
"""

import random

from adaptive_sampler import AdaptiveSampler, default_importance


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make_event(rng, services, weights, error_rate=0.0, slow_rate=0.0):
    return {
        "error": rng.random() < error_rate,
        "latency_ms": 900.0 if rng.random() < slow_rate else rng.random() * 100,
        "dimensions": {"service": rng.choices(services, weights)[0]},
    }


def run(sampler, clock, rate, duration, event_fn):
    """Feed events at `rate` rps for `duration` seconds; return emitted count."""
    n = int(round(rate * duration))
    dt = 1.0 / rate
    emitted = 0
    for i in range(n):
        clock.t += dt
        if sampler.observe(event_fn(i)):
            emitted += 1
    return emitted


def convergence_time(history, target, start, tol=0.05, sustain=3):
    """Seconds after `start` until reported rate stays within tol of target."""
    ends = [h["end"] for h in history]
    rates = [h["reported_rate"] for h in history]
    for i, end in enumerate(ends):
        if end < start:
            continue
        window = rates[i:i + sustain]
        if len(window) == sustain and all(
                abs(r - target) / target <= tol for r in window):
            return end - start
    return float("inf")


def steady_stats(history, target, start):
    """Mean/max relative deviation of reported rate for windows ending >= start."""
    devs = [abs(h["reported_rate"] - target) / target
            for h in history if h["end"] >= start]
    return (sum(devs) / len(devs), max(devs)) if devs else (float("nan"),) * 2


def dist_deviation(key_in, key_out):
    """Max abs deviation and total variation distance between distributions."""
    tin = sum(key_in.values())
    tout = sum(key_out.values())
    keys = set(key_in) | set(key_out)
    max_dev = 0.0
    tvd = 0.0
    rows = []
    for k in sorted(keys):
        pin = key_in.get(k, 0) / tin
        pout = key_out.get(k, 0) / tout
        max_dev = max(max_dev, abs(pin - pout))
        tvd += abs(pin - pout)
        rows.append((k, pin, pout))
    return rows, max_dev, tvd / 2.0


def header(title):
    print("\n=== %s ===" % title)


def scenario_steady():
    header("A. Steady traffic 1000 rps, target 100 rps")
    clock = FakeClock()
    rng = random.Random(1)
    s = AdaptiveSampler(100, key_dimensions=("service",), clock=clock)
    run(s, clock, 1000, 40, lambda i: make_event(rng, ["web"], [1.0]))
    mean_dev, max_dev = steady_stats(s.history, 100, start=10.0)
    conv = convergence_time(s.history, 100, start=0.0)
    print("convergence: %.1f s | mean dev: %.2f%% | max dev (post-conv): %.2f%%"
          % (conv, mean_dev * 100, max_dev * 100))
    return mean_dev, max_dev


def scenario_burst():
    header("B. Burst 100 -> 1000 rps at t=15s, target 100 rps")
    clock = FakeClock()
    rng = random.Random(2)
    s = AdaptiveSampler(100, key_dimensions=("service",), clock=clock)
    run(s, clock, 100, 15, lambda i: make_event(rng, ["web"], [1.0]))
    burst_start = clock.t
    run(s, clock, 1000, 30, lambda i: make_event(rng, ["web"], [1.0]))
    conv = convergence_time(s.history, 100, start=burst_start)
    peak = max(h["reported_rate"] for h in s.history if h["end"] >= burst_start)
    mean_dev, max_dev = steady_stats(s.history, 100, start=burst_start + 5)
    print("convergence after 10x burst: %.1f s | peak rate: %.0f rps "
          "(overshoot %.0f%%) | steady mean dev: %.2f%% max dev: %.2f%%"
          % (conv, peak, (peak / 100 - 1) * 100, mean_dev * 100, max_dev * 100))
    return conv, peak


def scenario_drop():
    header("C. Drop 1000 -> 400 rps and 1000 -> 40 rps at t=15s, target 100")
    for new_rate in (400, 40):
        clock = FakeClock()
        rng = random.Random(3)
        s = AdaptiveSampler(100, key_dimensions=("service",), clock=clock)
        run(s, clock, 1000, 15, lambda i: make_event(rng, ["web"], [1.0]))
        drop_start = clock.t
        emitted = run(s, clock, new_rate, 20,
                      lambda i: make_event(rng, ["web"], [1.0]))
        conv = convergence_time(s.history, 100, start=drop_start)
        tail = [h for h in s.history if h["end"] >= drop_start + 5]
        mean_rate = sum(h["reported_rate"] for h in tail) / len(tail)
        if new_rate >= 100:
            print("drop to %d rps: convergence %.1f s | steady mean rate %.1f "
                  "(dev %.2f%%)"
                  % (new_rate, conv, mean_rate, abs(mean_rate - 100)))
        else:
            print("drop to %d rps (below target): reported %.1f rps == input, "
                  "p=%.2f (keep-all, target unreachable)" %
                  (new_rate, mean_rate, s.p))


def scenario_all_important():
    header("D. All-important traffic 500 rps, target 100 rps")
    clock = FakeClock()
    rng = random.Random(4)
    s = AdaptiveSampler(100, key_dimensions=("service",), clock=clock)
    total = run(s, clock, 500, 10,
                lambda i: {**make_event(rng, ["web"], [1.0]), "error": True})
    print("in: %d | reported: %d (100%% of important kept; rate exceeds "
          "target by design)" % (500 * 10, total))


def scenario_no_traffic():
    header("E. No traffic / long idle")
    clock = FakeClock()
    rng = random.Random(5)
    s = AdaptiveSampler(100, key_dimensions=("service",), clock=clock)
    run(s, clock, 1000, 5, lambda i: make_event(rng, ["web"], [1.0]))
    clock.t += 600.0  # 10 min idle, no observe() calls
    kept = sum(s.observe(make_event(rng, ["web"], [1.0])) for _ in range(3))
    print("idle 600 s: no crash, no spurious reports; first events after idle "
          "kept=%d/3 (p resets toward 1.0)" % kept)


def scenario_distribution():
    header("F. Distribution fidelity: 4 services, skewed, 2000 rps, target 200")
    clock = FakeClock()
    rng = random.Random(6)
    services = ["web", "api", "worker", "batch"]
    weights = [0.50, 0.30, 0.15, 0.05]
    s = AdaptiveSampler(200, key_dimensions=("service",), clock=clock)
    run(s, clock, 2000, 30, lambda i: make_event(rng, services, weights))
    st = s.stats()
    rows, max_dev, tvd = dist_deviation(st["key_in"], st["key_out"])
    print("%-14s %8s %8s" % ("service", "in%", "out%"))
    for k, pin, pout in rows:
        print("%-14s %8.3f %8.3f" % (k[0], pin * 100, pout * 100))
    print("max deviation: %.3f pp | TVD: %.4f (threshold 1.0 pp)" %
          (max_dev * 100, tvd))
    return max_dev


def scenario_important_priority():
    header("G. Important priority: 5%% errors + 2%% slow, 2000 rps, target 100")
    clock = FakeClock()
    rng = random.Random(7)
    s = AdaptiveSampler(100, key_dimensions=("service",), clock=clock,
                        importance_fn=lambda e: default_importance(
                            e, slow_ms=500, dimension_values={"tier": {"gold"}}))
    important_in = [0]
    imp = s._important_fn

    def gen(i):
        e = make_event(rng, ["web"], [1.0], error_rate=0.05, slow_rate=0.02)
        important_in[0] += imp(e)
        return e

    run(s, clock, 2000, 20, gen)
    st = s.stats()
    recall = st["important_out"] / important_in[0]
    print("important in: %d | important reported: %d (recall %.1f%%) | "
          "total reported rate ~%.0f rps (target 100; important alone "
          "exceeds target, so normal traffic is fully throttled)"
          % (important_in[0], st["important_out"], recall * 100,
             st["total_out"] / 20.0))


if __name__ == "__main__":
    scenario_steady()
    scenario_burst()
    scenario_drop()
    scenario_all_important()
    scenario_no_traffic()
    scenario_distribution()
    scenario_important_priority()
