# Wait-Time Distribution Report

- date: 2026-09-30 03:41:39 +0800
- python: 3.12.3
- semaphore config: {'permits': 4, 'threads_n': 48, 'ops_per_thread': 1500}
- barrier config: {'parties': 16, 'rounds': 2000}

## Fairness evidence

The semaphore serves waiters from a strict FIFO queue: a permit freed by release() is handed to the longest-queued waiter, so no thread can be overtaken once queued (proven deterministically by `test_fifo_wakeup_order`). Under stress this shows up as wait-time equity: the ratio of the slowest to the fastest per-thread mean wait is **1.00x** (min 33.322 ms, max 33.467 ms across 48 threads). The barrier releases each generation exactly once and hands out arrival indices 0..N-1 in arrival order.

No starvation: the longest semaphore wait is bounded (max/p50 = 2.4x), and every acquire completed well within the 30s starvation guard.

### Semaphore acquire wait (ms)

- samples: 72000
- min / p50 / p90 / p99 / max (ms): 0.001 / 31.518 / 40.276 / 59.045 / 77.040
- mean / stdev (ms): 33.406 / 6.909
- max/median ratio: 2.4


### Barrier wait (ms)

- samples: 32000
- min / p50 / p90 / p99 / max (ms): 0.154 / 0.844 / 1.040 / 1.464 / 5.834
- mean / stdev (ms): 0.854 / 0.249
- max/median ratio: 6.9


## Histograms

### semaphore wait time histogram (ms)
```
    0.001 -     9.631 |     149 #
    9.631 -    19.261 |     119 #
   19.261 -    28.891 |    6491 #####
   28.891 -    38.520 |   57233 ###############################################
   38.520 -    48.150 |    2568 ##
   48.150 -    57.780 |    4519 ###
   57.780 -    67.410 |     829 #
   67.410 -    77.040 |      92 #
```

### barrier wait time histogram (ms)
```
    0.154 -     0.864 |   19261 ####################################
    0.864 -     1.574 |   12516 #######################
    1.574 -     2.284 |     163 #
    2.284 -     2.994 |      25 #
    2.994 -     3.704 |       6 #
    3.704 -     4.414 |      10 #
    4.414 -     5.124 |       2 #
    5.124 -     5.834 |      17 #
```
