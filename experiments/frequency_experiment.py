#!/usr/bin/env python3
"""频率实验：固定随机源下，random.choices 的经验频率 vs 理论概率。

被测对象: CPython 3.12.3 标准库 random.Random.choices
          (/usr/lib/python3.12/random.py:454)
运行方式: python3 experiments/frequency_experiment.py
仅使用标准库；所有用例使用固定种子，结果可复现。
"""

import random
from collections import Counter

SEED = 20260930

# 卡方分布临界值（常用自由度，右尾）
CHI2_CRIT = {
    1: (3.841, 6.635),
    2: (5.991, 9.210),
    3: (7.815, 11.345),
    4: (9.488, 13.277),
}


def chi2_stat(observed, expected):
    return sum((o - e) ** 2 / e for o, e in zip(observed, expected) if e > 0)


def run_case(title, population, weights, n_draws, seed=SEED, use_weights=True):
    rng = random.Random(seed)
    if use_weights:
        draws = rng.choices(population, weights=weights, k=n_draws)
    else:
        draws = rng.choices(population, k=n_draws)
    counts = Counter(draws)
    total_w = sum(weights) if use_weights else len(population)
    print(f"\n=== {title} ===")
    print(f"seed={seed}  n={n_draws:,}")
    header = f"{'item':>10} {'weight':>12} {'理论概率':>12} {'经验频率':>12} {'偏差(pp)':>10} {'计数':>12} {'期望计数':>12}"
    print(header)
    observed, expected = [], []
    for i, item in enumerate(population):
        w = weights[i] if use_weights else 1
        p_theo = w / total_w
        p_emp = counts.get(item, 0) / n_draws
        observed.append(counts.get(item, 0))
        expected.append(p_theo * n_draws)
        print(f"{str(item):>10} {w:>12} {p_theo:>12.6f} {p_emp:>12.6f} "
              f"{(p_emp - p_theo) * 100:>10.4f} {counts.get(item, 0):>12,} {p_theo * n_draws:>12,.1f}")
    stat = chi2_stat(observed, expected)
    df = len(population) - 1
    crit = CHI2_CRIT.get(df)
    verdict = ""
    if crit:
        verdict = "通过(无系统偏差证据)" if stat < crit[0] else ("存疑" if stat < crit[1] else "拒绝(存在系统偏差)")
        verdict += f"  [临界值 5%={crit[0]}, 1%={crit[1]}]"
    print(f"卡方统计量={stat:.3f}  df={df}  => {verdict}")


def main():
    # 用例1: 普通相对权重 [1,2,3,4] -> 理论概率 0.1/0.2/0.3/0.4
    run_case("用例1 普通权重 [1,2,3,4]", list("abcd"), [1, 2, 3, 4], 1_000_000)

    # 用例2: 等权重快速路径 (weights=None, 源码 L466-467)
    run_case("用例2 等权重快速路径 weights=None", list("abcd"), None, 1_000_000, use_weights=False)

    # 用例3: 权重相差百万倍 [1e6, 1e6, 1]
    run_case("用例3 权重相差百万倍 [1e6, 1e6, 1]", ["big1", "big2", "tiny"],
             [1_000_000, 1_000_000, 1], 10_000_000)

    # 用例4: 负权重混入（文档未禁止，代码也未校验）——观察是否静默偏差
    run_case("用例4 负权重 [5,-1,5]（校验缺失演示）", list("abc"), [5, -1, 5], 900_000)


if __name__ == "__main__":
    main()
