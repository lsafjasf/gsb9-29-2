# Differential Report: legacy call sites vs unified component

Same random source injected into both sides; samples must be identical.

| Call site | Unified strategy | Cases | Mismatches | Verdict |
|---|---|---|---|---|
| lottery.pick_winners | UniformSampling | 200 | 0 | PASS |
| promo.draw_by_weight | WeightedSampling(replace=True) | 200 | 0 | PASS |
| report.stratified_sample | StratifiedSampling | 3 | 0 | PASS |

## Intentional divergences (legacy bugs fixed by the unified component)

| Scenario | Legacy behaviour | Unified behaviour |
|---|---|---|
| lottery: k > population | returns all 5 records silently | SampleSizeError: sample size 9 exceeds population size 5 (sampling without replacement) |
| report: fractional quotas (1.5 + 1.5) | truncates, returns 2 of 3 rows | largest-remainder, returns 3 of 3 rows |
| promo: zero-weight item, rng draws 0.0 | selects zero-weight item ['z'] | never selects it: ['a'] |
| promo: all-zero weights | ZeroDivisionError | WeightError: total weight must be positive (all weights are zero) |
