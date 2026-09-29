"""Generate reports/unbiasedness.md.

For every strategy, draw many samples with a fixed seed and tabulate
observed frequencies against theoretical probabilities, with a 6-sigma
binomial standard-error band per entry.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sampling
from sampling.verify import (
    binomial_tolerance,
    inclusion_frequencies,
    position_frequencies,
)

SEED = 20260930


def table(title, note, observed, expected, trials):
    lines = ["## %s" % title, "", note, ""]
    lines.append("| Item | Observed | Theoretical | |diff| | 6-sigma tol | Within |")
    lines.append("|---|---|---|---|---|---|")
    ok = True
    for item in expected:
        obs, exp = observed[item], expected[item]
        tol = binomial_tolerance(exp, trials)
        within = abs(obs - exp) <= tol
        ok = ok and within
        lines.append(
            "| %s | %.5f | %.5f | %.5f | %.5f | %s |"
            % (item, obs, exp, abs(obs - exp), tol, "yes" if within else "NO")
        )
    lines.append("")
    return lines, ok


def main():
    out = ["# Unbiasedness Report", ""]
    out.append("Seed %d throughout; fully reproducible via "
               "`python3 tools/unbiasedness_report.py`." % SEED)
    out.append("")
    all_ok = True

    items = list(range(10))
    k, trials = 4, 60_000
    rng = sampling.SeededSource(SEED)
    s = sampling.UniformSampling()
    draws = [s.sample(items, k, rng=rng) for _ in range(trials)]
    lines, ok = table(
        "Uniform (without replacement), n=10, k=4, %d draws" % trials,
        "Theoretical inclusion probability per item: k/n = 0.4.",
        inclusion_frequencies(draws, items),
        {i: k / len(items) for i in items},
        trials,
    )
    out += lines
    all_ok &= ok

    items, weights = ["a", "b", "c"], [1, 2, 1]
    total = float(sum(weights))
    k, trials = 3, 40_000
    rng = sampling.SeededSource(SEED)
    s = sampling.WeightedSampling(weights, replace=True)
    draws = [s.sample(items, k, rng=rng) for _ in range(trials)]
    lines, ok = table(
        "Weighted (with replacement), weights %s, %d draws of k=3" % (weights, trials),
        "Theoretical share of sampled positions per item: w_i / sum(w).",
        position_frequencies(draws, items),
        {i: w / total for i, w in zip(items, weights)},
        trials * k,
    )
    out += lines
    all_ok &= ok

    items, weights = ["a", "b", "c"], [3, 1, 4]
    total = float(sum(weights))
    trials = 40_000
    rng = sampling.SeededSource(SEED)
    s = sampling.WeightedSampling(weights, replace=False)
    firsts = [[s.sample(items, 2, rng=rng)[0]] for _ in range(trials)]
    lines, ok = table(
        "Weighted (without replacement, A-Res), weights %s, %d draws" % (weights, trials),
        "Theoretical probability of being the first selected item: w_i / sum(w).",
        inclusion_frequencies(firsts, items),
        {i: w / total for i, w in zip(items, weights)},
        trials,
    )
    out += lines
    all_ok &= ok

    rows = [{"g": "A", "id": i} for i in range(10)]
    rows += [{"g": "B", "id": 100 + i} for i in range(5)]
    k, trials = 6, 40_000
    rng = sampling.SeededSource(SEED)
    s = sampling.StratifiedSampling(key=lambda r: r["g"])
    draws = [s.sample(rows, k, rng=rng) for _ in range(trials)]
    ids = [r["id"] for r in rows]
    expected = {r["id"]: (4 / 10 if r["g"] == "A" else 2 / 5) for r in rows}
    lines, ok = table(
        "Stratified, strata sizes 10/5, k=6 (quotas 4/2), %d draws" % trials,
        "Theoretical inclusion probability per item: quota_g / size_g.",
        inclusion_frequencies([[x["id"] for x in d] for d in draws], ids),
        expected,
        trials,
    )
    out += lines
    all_ok &= ok

    n, k, trials = 50, 5, 20_000
    rng = sampling.SeededSource(SEED)
    draws = [sampling.reservoir_sample(iter(range(n)), k, rng=rng)
             for _ in range(trials)]
    lines, ok = table(
        "Streaming reservoir (uniform), n=50, k=5, %d draws" % trials,
        "Theoretical inclusion probability per item: k/n = 0.1.",
        inclusion_frequencies(draws, list(range(n))),
        {i: k / n for i in range(n)},
        trials,
    )
    out += lines
    all_ok &= ok

    out.append("Overall: **%s**" % ("PASS" if all_ok else "FAIL"))
    out.append("")
    path = os.path.join(os.path.dirname(__file__), "..", "reports", "unbiasedness.md")
    with open(path, "w") as fh:
        fh.write("\n".join(out))
    print("wrote %s" % os.path.relpath(path))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
