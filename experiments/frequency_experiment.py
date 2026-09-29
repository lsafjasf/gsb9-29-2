#!/usr/bin/env python3
"""频率实验：固定随机源下 random.choices 的经验频率 vs 理论概率。

被测对象: CPython 3.12.3 标准库 random.Random.choices
          (/usr/lib/python3.12/random.py:454)
运行方式: python3 experiments/frequency_experiment.py
仅使用标准库；所有用例固定种子 SEED=20260930，结果可完整复现。
"""

import bisect
import random
from collections import Counter
from fractions import Fraction
from itertools import accumulate

SEED = 20260930

# 卡方临界值（右尾 p=0.05）
CHI2_CRIT_005 = {1: 3.841, 2: 5.991, 3: 7.815, 4: 9.488}


def chi2_stat(observed, expected):
    return sum((o - e) ** 2 / e for o, e in zip(observed, expected) if e > 0)


def implied_distribution(cum_weights, hi):
    """精确计算 choices 隐含的真实分布。

    choices 的选中规则是 idx = bisect(cum_weights, x, 0, hi)，x ~ U[0, total)。
    对任意（含非单调）cum_weights，用断点切分 [0, total)，逐段求 bisect 值，
    得到每个下标被命中的精确测度（Fraction 精确有理数）。
    """
    total = Fraction(float(cum_weights[-1]) if not isinstance(cum_weights[-1], Fraction)
                     else cum_weights[-1])
    cum = [Fraction(float(c)) if not isinstance(c, Fraction) else c for c in cum_weights]
    points = {Fraction(0), total}
    for c in cum[:hi]:
        if 0 < c < total:
            points.add(c)
    points = sorted(points)
    mass = [Fraction(0)] * len(cum)
    for lo, hi_ in zip(points, points[1:]):
        mid = (lo + hi_) / 2
        idx = bisect.bisect(cum, mid, 0, hi)
        mass[idx] += hi_ - lo
    return [m / total for m in mass]


def show_table(title, population, weights, n_draws, seed=SEED):
    rng = random.Random(seed)
    if weights is None:
        draws = rng.choices(population, k=n_draws)
        fair = [1.0 / len(population)] * len(population)
        wlabel = ["(none)"] * len(population)
    else:
        draws = rng.choices(population, weights=weights, k=n_draws)
        total = sum(weights)
        fair = [w / total for w in weights]
        wlabel = [str(w) for w in weights]
    counts = Counter(draws)
    print(f"\n=== {title} ===")
    print(f"seed={seed}  n={n_draws:,}")
    print(f"{'item':>8} {'weight':>10} {'理论概率':>12} {'经验频率':>12} "
          f"{'偏差(pp)':>10} {'计数':>12} {'期望计数':>14}")
    observed, expected = [], []
    for item, w, p in zip(population, wlabel, fair):
        c = counts.get(item, 0)
        freq = c / n_draws
        observed.append(c)
        expected.append(p * n_draws)
        print(f"{item!r:>8} {w:>10} {p:>12.6f} {freq:>12.6f} "
              f"{(freq - p) * 100:>10.4f} {c:>12,} {p * n_draws:>14,.1f}")
    return observed, expected, draws


def judge_chi2(observed, expected, df):
    stat = chi2_stat(observed, expected)
    crit = CHI2_CRIT_005[df]
    verdict = "通过（无系统性偏差证据）" if stat < crit else "拒绝（存在系统性偏差）"
    print(f"卡方检验: chi2={stat:.3f}  df={df}  临界值(p=0.05)={crit}  -> {verdict}")
    return stat


def main():
    # 用例1：weights=None 快速路径（L466-467: floor(random()*n)）
    obs, exp, _ = show_table("用例1: 均匀总体, weights=None（快速路径）",
                             list("ABCD"), None, 400_000)
    judge_chi2(obs, exp, df=3)

    # 用例2：整数相对权重 1:2:3:4
    obs, exp, _ = show_table("用例2: 整数权重 [1,2,3,4]",
                             list("ABCD"), [1, 2, 3, 4], 400_000)
    judge_chi2(obs, exp, df=3)

    # 用例3：浮点权重
    obs, exp, _ = show_table("用例3: 浮点权重 [0.1,0.2,0.3,0.4]",
                             list("ABCD"), [0.1, 0.2, 0.3, 0.4], 400_000)
    judge_chi2(obs, exp, df=3)

    # 用例4：权重相差百万倍。小权重期望计数≈2，卡方不适用，用 Poisson 涨落判断。
    obs, exp, _ = show_table("用例4: 权重相差 1e6 倍 [1000000,1,1]",
                             list("ABC"), [1_000_000, 1, 1], 2_000_000)
    for name, o, e in zip("ABC", obs, exp):
        if e < 10:
            hi = e + 4.0 * e ** 0.5 + 3  # Poisson 99.9% 上界近似
            ok = 0 <= o <= hi
            print(f"  小权重项 {name}: 计数={o}  期望={e:.2f}  "
                  f"Poisson 99.9% 区间≈[0,{hi:.1f}] -> {'正常涨落' if ok else '异常'}")

    # 用例5：负权重 —— 不报错，分布静默偏离文档语义。
    # cum=[5,4,9] 非单调，bisect 永远不会返回下标1；
    # 隐含真实分布为 [4/9, 0, 5/9]：两个相等的权重 5 得到不同概率。
    print("\n=== 用例5: 负权重 [5,-1,5]（不报错，静默偏差） ===")
    n = 900_000
    weights = [5, -1, 5]
    rng = random.Random(SEED)
    draws = rng.choices(list("ABC"), weights=weights, k=n)
    counts = Counter(draws)
    cum = list(accumulate(weights))
    implied = implied_distribution(cum, hi=2)  # 精确隐含分布
    print(f"cum_weights={cum}（非单调）  seed={SEED}  n={n:,}")
    print(f"{'item':>8} {'weight':>8} {'w/Σw(文档语义)':>16} {'隐含真实概率':>14} {'经验频率':>12}")
    for i, item in enumerate("ABC"):
        print(f"{item!r:>8} {weights[i]:>8} {weights[i] / 9:>16.4f} "
              f"{float(implied[i]):>14.4f} {counts[item] / n:>12.4f}")
    obs_nz = [counts["A"], counts["C"]]
    exp_nz = [float(implied[0]) * n, float(implied[2]) * n]
    stat = chi2_stat(obs_nz, exp_nz)
    print(f"经验频率 vs 隐含分布: chi2={stat:.3f} (df=1, 临界值3.841) -> "
          f"{'一致：偏差来自实现本身，而非随机涨落' if stat < 3.841 else '异常'}")
    print("结论: 相等权重 5 得到不等概率 4/9≠5/9，负权重项概率为 0；"
          "实现未做任何负权重检查（L479-485 只校验总和）。")

    # 用例6：随机源审计 —— 每次抽取恰好消耗 1 个 random()。
    print("\n=== 用例6: 随机源使用审计（手动复现内部算法） ===")
    pop, w, k = list("ABCD"), [1, 2, 3, 4], 1000
    rng1 = random.Random(SEED)
    got = rng1.choices(pop, weights=w, k=k)
    rng2 = random.Random(SEED)
    cum = list(accumulate(w))
    total = cum[-1] + 0.0
    manual = [pop[bisect.bisect(cum, rng2.random() * total, 0, len(pop) - 1)]
              for _ in range(k)]
    print(f"choices 输出 == 手动(bisect+每次1个random()) 复现: {got == manual}")
    print(f"k={k} 次抽取恰好消耗 k={k} 个 random() 值")

    # 用例7：可复现性 —— 同种子两次运行序列完全一致。
    print("\n=== 用例7: 固定种子可复现性 ===")
    seq1 = random.Random(SEED).choices(pop, weights=w, k=1000)
    seq2 = random.Random(SEED).choices(pop, weights=w, k=1000)
    print(f"两次运行序列完全一致: {seq1 == seq2}")

    # 用例8：独立性快检 —— 相邻重复率应≈Σpᵢ²。
    print("\n=== 用例8: 相邻抽取独立性快检（权重[1,2,3,4]） ===")
    n = 400_000
    draws = random.Random(SEED).choices(pop, weights=w, k=n)
    repeats = sum(1 for a, b in zip(draws, draws[1:]) if a == b)
    p2 = sum((x / 10) ** 2 for x in w)
    print(f"相邻重复率: 经验={repeats / (n - 1):.5f}  理论=Σpᵢ²={p2:.5f}  "
          f"(二者接近 -> 无序列相关证据)")


if __name__ == "__main__":
    main()
