# Error Regression Report: sample_report

Generated: 2026-10-02T21:00:41+00:00

**Overall: FAIL** (4 passed, 3 failed, 0 errors, 7 total)

| Status | Case | Kind | Trials | Metric detail |
| --- | --- | --- | --- | --- |
| PASS | `sqrt2_newton` | deterministic | 5 | `abs_error[max]: observed=3.187175e-16 baseline=2.381568e-16+-9.96e-17 (n=12) limit=8.063880e-14 z=+0.00 sigma_eff=2.01e-14`<br>`rel_error[max]: observed=2.253673e-16 baseline=1.684023e-16+-7.04e-17 (n=12) limit=8.056556e-14 z=+0.00 sigma_eff=2.01e-14`<br>`iterations: mean_iters=6.00 baseline=6.00+-0.00 (n=12) limit=9.50`<br>`elapsed: median=1.4us baseline=0.9us ratio=1.49 limit=1.8us` |
| PASS | `sqrt2_newton_exact` | deterministic | 3 | `abs_error[max]: observed=1.253717e-16 baseline=1.253717e-16+-0.00e+00 (n=6) limit=8.052037e-14 z=+0.00 sigma_eff=2.01e-14`<br>`rel_error[max]: observed=8.865116e-17 baseline=8.865116e-17+-0.00e+00 (n=6) limit=8.048182e-14 z=+0.00 sigma_eff=2.01e-14`<br>`iterations: mean_iters=6.00 baseline=6.00+-0.00 (n=6) limit=9.50`<br>`elapsed: median=0.6us baseline=0.6us ratio=1.09 limit=1.2us` |
| PASS | `sqrt2_newton_sparse` | deterministic | 3 | `abs_error[max]: observed=3.187175e-16 baseline=3.187175e-16+-0.00e+00 (n=2) limit=7.835743e-13 z=+0.00 sigma_eff=2.01e-14`<br>`rel_error[max]: observed=2.253673e-16 baseline=2.253673e-16+-0.00e+00 (n=2) limit=7.834763e-13 z=+0.00 sigma_eff=2.01e-14`<br>`iterations: mean_iters=6.00 baseline=6.00+-0.00 (n=2) limit=9.50`<br>`elapsed: median=1.1us baseline=2.2us ratio=0.49 limit=4.5us` |
|  |  |  |  | ⚠ baseline built from only 2 samples (< 5); confidence interval widened via Student-t, gather more data |
| PASS | `pi_monte_carlo` | randomized | 30 | `abs_error[mean]: mean=2.415837e-03 vs baseline=4.023959e-03 (n=30/30) diff=-1.608e-03 delta=4.024e-04 t=-3.00 crit=2.40 df=55.8 lower_bound=-3.214e-03`<br>`rel_error[mean]: mean=7.689850e-04 vs baseline=1.280866e-03 (n=30/30) diff=-5.119e-04 delta=1.281e-04 t=-3.00 crit=2.40 df=55.8 lower_bound=-1.023e-03`<br>`iterations: mean_iters=20000.00 baseline=20000.00+-0.00 (n=30) limit=25002.00`<br>`elapsed: median=911.7us baseline=938.8us ratio=0.97 limit=1877.6us` |
| FAIL | `sqrt2_newton_early_stop` | deterministic | 5 | `FAIL abs_error[max]: observed=2.123901e-06 baseline=2.381568e-16+-9.96e-17 (n=12) limit=8.063880e-14 z=+105681545.77 sigma_eff=2.01e-14`<br>`FAIL rel_error[max]: observed=1.501825e-06 baseline=1.684023e-16+-7.04e-17 (n=12) limit=8.056556e-14 z=+74728137.66 sigma_eff=2.01e-14`<br>`iterations: mean_iters=3.00 baseline=6.00+-0.00 (n=12) limit=9.50`<br>`elapsed: median=0.9us baseline=0.9us ratio=0.96 limit=1.8us` |
| FAIL | `sqrt2_newton_extra_iters` | deterministic | 5 | `abs_error[max]: observed=3.187175e-16 baseline=2.381568e-16+-9.96e-17 (n=12) limit=8.063880e-14 z=+0.00 sigma_eff=2.01e-14`<br>`rel_error[max]: observed=2.253673e-16 baseline=1.684023e-16+-7.04e-17 (n=12) limit=8.056556e-14 z=+0.00 sigma_eff=2.01e-14`<br>`FAIL iterations: mean_iters=14.00 baseline=6.00+-0.00 (n=12) limit=9.50`<br>`elapsed: median=1.2us baseline=0.9us ratio=1.36 limit=1.8us` |
| FAIL | `pi_monte_carlo_biased` | randomized | 30 | `FAIL abs_error[mean]: mean=2.207911e-02 vs baseline=4.023959e-03 (n=30/30) diff=+1.806e-02 delta=4.024e-04 t=+22.56 crit=2.39 df=57.2 lower_bound=+1.618e-02`<br>`FAIL rel_error[mean]: mean=7.027999e-03 vs baseline=1.280866e-03 (n=30/30) diff=+5.747e-03 delta=1.281e-04 t=+22.56 crit=2.39 df=57.2 lower_bound=+5.151e-03`<br>`iterations: mean_iters=20000.00 baseline=20000.00+-0.00 (n=30) limit=25002.00`<br>`elapsed: median=946.6us baseline=938.8us ratio=1.01 limit=1877.6us` |

## Legend

- `abs_error[max]`: worst absolute error over fresh trials vs the statistical limit from the baseline (deterministic cases).
- `abs_error[mean]`: Welch one-sided test of the mean error against the non-inferiority margin `delta` (randomized cases); fails only when the lower (1-alpha) confidence bound on degradation exceeds `delta`.
- `iterations`: failure stops accuracy being bought silently with more iterations.
- `elapsed`: warning only; wall-clock noise is not treated as a correctness regression.
