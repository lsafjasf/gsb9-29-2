#!/usr/bin/env python3
"""精度退化实验：权重动态范围超过 float64 精度时，random.choices 如何失效。

被测对象: CPython 3.12.3 标准库 random.Random.choices
          (/usr/lib/python3.12/random.py:454)
关键机理:
  L469  cum_weights = list(_accumulate(weights))   # 前缀和
  L481  total = cum_weights[-1] + 0.0              # 强制转 float64
  L488  bisect(cum_weights, random() * total, ...) # random() 粒度 2**-53
运行方式: python3 experiments/precision_degradation.py
"""

import random
from itertools import accumulate

SEED = 20260930
N = 10_000_000


def banner(title):
    print(f"\n=== {title} ===")


def freq(weights, labels, n=N, seed=SEED):
    rng = random.Random(seed)
    draws = rng.choices(labels, weights=weights, k=n)
    return {x: draws.count(x) for x in dict.fromkeys(labels)}


banner("退化1 权重比 >= 2**53 且大权值在前：小权值元素概率坍缩为 0")
w = [2**53, 1]
cum = list(accumulate(w))
total = float(cum[-1])
print(f"weights      = {w}")
print(f"cum_weights  = {cum}            (int 精确)")
print(f"total(float) = {total!r}  (2**53+1 被舍入为 2**53)")
print(f"random()*total 的取值粒度 = 2**-53 * 2**53 = 1 -> 只产生整数,"
      f"且恒 < cum_weights[0]={cum[0]}")
print(f"=> bisect 恒返回 0, 第二个元素理论概率 1/(2**53+1)≈1.1e-16, 实际为 0")
counts = freq(w, ["big", "tiny"])
print(f"频率实验(n={N:,}, seed={SEED}): {counts}  <- tiny 一次都抽不到")


banner("退化2 同样的权重换个顺序：小权值元素概率被系统性放大")
for w in ([1, 2**54], [1, 2**60]):
    cum = list(accumulate(w))
    total = float(cum[-1])
    granularity = total * 2**-53          # random()*total 的取值间隔
    p_actual = 2**-53                      # P(x < cum[0]=1) = P(x == 0) = P(random()==0)
    p_theo = w[0] / (w[0] + w[1])
    print(f"weights      = {w}")
    print(f"  total(float) = {total!r}, random()*total 粒度 = {granularity:.1f}"
          f" (x 只取 {granularity:.0f} 的整数倍)")
    print(f"  tiny 理论概率 = {p_theo:.3e}, 实际概率 = {p_actual:.3e},"
          f" 相对放大 {p_actual / p_theo:.0f} 倍")
rng = random.Random(SEED)
samples = [rng.random() * float(2**54) for _ in range(5)]
print(f"  机理复现: total=2**54 时 random()*total 抽样值 = {samples}")
print(f"  全部为 2 的倍数 => x==0 的概率确为 2**-53 (与上面解析一致)")


banner("退化3 浮点权重前缀和坍缩：cum_weights 相邻元素相等")
w = [1e16, 1.0, 1.0]
cum = list(accumulate(w))
print(f"weights      = {w}")
print(f"cum_weights  = {cum}  <- 1e16+1.0 被舍入回 1e16 (float64 在 1e16 处 ulp=2)")
print("后两个元素的累计区间为空 => 理论概率各 ~5e-17, 实际不可达")
counts = freq(w, ["big", "tiny1", "tiny2"])
print(f"频率实验(n={N:,}, seed={SEED}): {counts}")


banner("对照 权重比 1e6（百万倍）：float64 精度充足，不退化")
w = [1_000_000, 1_000_000, 1]
cum = list(accumulate(w))
print(f"cum_weights = {cum}, ulp(2e6)={2.0**-52 * 2**21:.2e} << 最小权重 1")
counts = freq(w, ["big1", "big2", "tiny"])
print(f"频率实验(n={N:,}, seed={SEED}): {counts}  (tiny 期望 {N / (sum(w)):.1f} 次)")


banner("退化4 等权重快速路径：总体大小 n > 2**53 时部分元素不可达（理论性）")
print("L466-467: floor(random() * (n+0.0)); random() 粒度 2**-53,")
print("n > 2**53 时 random()*n 的粒度 > 1, 部分下标永远落不到。")
print("本机无法构造 2**53 个元素, 仅作源码级推断, 标记为理论风险。")
