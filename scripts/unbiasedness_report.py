#!/usr/bin/env python3
"""生成无偏性验证报告：各策略经验频率 vs 理论概率，写入 docs/UNBIASEDNESS.md。

用法：python3 scripts/unbiasedness_report.py
"""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sampling import (  # noqa: E402
    Sampler, UniformSampling, WeightedSampling, StratifiedSampling,
    SeededRandom, reservoir_uniform, reservoir_weighted, largest_remainder,
    chi2_statistic, chi2_sf,
)

SEED = 20260930


def fmt_p(p):
    return "%.4g" % p


def chi2_row(observed, expected):
    stat = chi2_statistic(observed, expected)
    df = sum(1 for e in expected if e > 0) - 1
    return stat, df, chi2_sf(stat, df)


def section_uniform():
    n, k, trials = 8, 3, 120000
    sampler = Sampler(UniformSampling(), SeededRandom(SEED))
    counts = Counter()
    for _ in range(trials):
        counts.update(sampler.sample(list(range(n)), k))
    p = k / n
    lines = [
        "## 1. 等概率策略（UniformSampling）",
        "",
        "总体 n=%d，样本量 k=%d，试验 %d 次，理论包含概率 k/n = %.4f。" % (n, k, trials, p),
        "",
        "| 条目 | 理论概率 | 经验频率 | 偏差 |",
        "|---|---|---|---|",
    ]
    for i in range(n):
        freq = counts[i] / trials
        lines.append("| %d | %.4f | %.4f | %+.5f |" % (i, p, freq, freq - p))
    stat, df, pv = chi2_row([counts[i] for i in range(n)], [trials * p] * n)
    lines += [
        "",
        "卡方统计量 %.3f（df=%d），p 值 %s。" % (stat, df, fmt_p(pv)),
        "",
    ]
    return lines


def section_weighted():
    weights = [5.0, 3.0, 2.0, 0.0]
    trials = 120000
    total = sum(weights)
    sampler = Sampler(WeightedSampling(weights), SeededRandom(SEED))
    counts = Counter()
    for _ in range(trials):
        counts.update(sampler.sample(list(range(4)), 1))
    lines = [
        "## 2. 按权重策略（WeightedSampling，k=1）",
        "",
        "权重 %s，试验 %d 次，理论概率 w_i/Σw。" % (weights, trials),
        "",
        "| 条目 | 权重 | 理论概率 | 经验频率 | 偏差 |",
        "|---|---|---|---|---|",
    ]
    for i, w in enumerate(weights):
        p = w / total
        freq = counts[i] / trials
        lines.append("| %d | %.1f | %.4f | %.4f | %+.5f |" % (i, w, p, freq, freq - p))
    stat, df, pv = chi2_row([counts[i] for i in range(4)],
                            [trials * w / total for w in weights])
    lines += [
        "",
        "卡方统计量 %.3f（df=%d），p 值 %s。零权重条目出现 %d 次（理论 0 次）。"
        % (stat, df, fmt_p(pv), counts[3]),
        "",
    ]
    return lines


def section_weighted_k2():
    weights = [5.0, 3.0, 2.0, 0.0]
    total = sum(weights)
    probs = [w / total for w in weights]
    # k=2 无放回的精确理论包含概率：P_i = p_i + Σ_{j≠i} p_j * p_i/(1-p_j)
    theory = []
    for i in range(4):
        pi = probs[i]
        theory.append(pi + sum(probs[j] * pi / (1 - probs[j])
                               for j in range(4) if j != i and probs[j] < 1))
    trials = 120000
    sampler = Sampler(WeightedSampling(weights), SeededRandom(SEED))
    counts = Counter()
    for _ in range(trials):
        counts.update(sampler.sample(list(range(4)), 2))
    lines = [
        "## 3. 按权重策略（WeightedSampling，k=2 无放回）",
        "",
        "权重 %s，试验 %d 次。理论包含概率按序贯无放回精确计算："
        "P_i = p_i + Σ_{j≠i} p_j·p_i/(1−p_j)。" % (weights, trials),
        "",
        "| 条目 | 理论包含概率 | 经验频率 | 偏差 |",
        "|---|---|---|---|",
    ]
    for i in range(4):
        freq = counts[i] / trials
        lines.append("| %d | %.4f | %.4f | %+.5f |" % (i, theory[i], freq, freq - theory[i]))
    stat, df, pv = chi2_row([counts[i] for i in range(4)],
                            [trials * t for t in theory])
    lines += [
        "",
        "卡方统计量 %.3f（df=%d），p 值 %s。" % (stat, df, fmt_p(pv)),
        "",
    ]
    return lines


def section_stratified():
    sizes = [55, 30, 15]
    k = 10
    alloc = largest_remainder(sizes, k)
    pop = [(s, i) for s, size in enumerate(sizes) for i in range(size)]
    trials = 60000
    sampler = Sampler(StratifiedSampling(lambda x: x[0]), SeededRandom(SEED))
    stratum_counts = Counter()
    item_counts = Counter()
    for _ in range(trials):
        got = sampler.sample(pop, k)
        stratum_counts.update(rec[0] for rec in got)
        item_counts.update(got)
    lines = [
        "## 4. 分层策略（StratifiedSampling）",
        "",
        "层大小 %s，k=%d，最大余数法分配 %s，试验 %d 次。"
        % (sizes, k, alloc, trials),
        "",
        "层级包含次数：",
        "",
        "| 层 | 层大小 | 分配名额 | 理论包含概率(层内) | 层均经验频率 | 偏差 |",
        "|---|---|---|---|---|---|",
    ]
    for s, size in enumerate(sizes):
        p = alloc[s] / size
        freq = stratum_counts[s] / trials / size
        lines.append("| %d | %d | %d | %.4f | %.4f | %+.5f |"
                     % (s, size, alloc[s], p, freq, freq - p))
    # 层内均匀性：对每层做条目级卡方
    lines += ["", "层内条目级均匀性（每层一个卡方检验）：", "",
              "| 层 | 卡方统计量 | df | p 值 |", "|---|---|---|---|"]
    for s, size in enumerate(sizes):
        observed = [item_counts[(s, i)] for i in range(size)]
        expected = [trials * alloc[s] / size] * size
        stat, df, pv = chi2_row(observed, expected)
        lines.append("| %d | %.3f | %d | %s |" % (s, stat, df, fmt_p(pv)))
    lines.append("")
    return lines


def section_stream():
    n, k, trials = 8, 3, 120000
    counts = Counter()
    for t in range(trials):
        counts.update(reservoir_uniform(iter(range(n)), k, SeededRandom(SEED + t)))
    p = k / n
    lines = [
        "## 5. 流式等概率水塘抽样（reservoir_uniform）",
        "",
        "总体 n=%d（迭代器输入），k=%d，试验 %d 次，理论包含概率 %.4f。"
        % (n, k, trials, p),
        "",
        "| 条目 | 理论概率 | 经验频率 | 偏差 |",
        "|---|---|---|---|",
    ]
    for i in range(n):
        freq = counts[i] / trials
        lines.append("| %d | %.4f | %.4f | %+.5f |" % (i, p, freq, freq - p))
    stat, df, pv = chi2_row([counts[i] for i in range(n)], [trials * p] * n)
    lines += [
        "",
        "卡方统计量 %.3f（df=%d），p 值 %s。" % (stat, df, fmt_p(pv)),
        "",
    ]
    return lines


def section_stream_weighted():
    pairs = [(i, w) for i, w in enumerate([5.0, 3.0, 2.0, 0.0])]
    k, trials = 1, 120000
    counts = Counter()
    for t in range(trials):
        counts.update(reservoir_weighted(iter(pairs), k, SeededRandom(SEED + t)))
    total = 10.0
    lines = [
        "## 6. 流式按权重水塘抽样（reservoir_weighted，A-Res，k=1）",
        "",
        "权重 [5, 3, 2, 0]，试验 %d 次，理论概率 w_i/Σw。" % trials,
        "",
        "| 条目 | 理论概率 | 经验频率 | 偏差 |",
        "|---|---|---|---|",
    ]
    for i, w in enumerate([5.0, 3.0, 2.0, 0.0]):
        p = w / total
        freq = counts[i] / trials
        lines.append("| %d | %.4f | %.4f | %+.5f |" % (i, p, freq, freq - p))
    stat, df, pv = chi2_row([counts[i] for i in range(4)],
                            [trials * w / total for w in [5.0, 3.0, 2.0, 0.0]])
    lines += [
        "",
        "卡方统计量 %.3f（df=%d），p 值 %s。零权重条目出现 %d 次。"
        % (stat, df, fmt_p(pv), counts[3]),
        "",
    ]
    return lines


def main():
    out = [
        "# 无偏性验证数据",
        "",
        "随机源统一为 `SeededRandom(%d)`（流式试验逐次 +t 递增），"
        "检验为 Pearson 卡方拟合优度，p 值越小越偏离理论分布；"
        "通常以 p > 0.001 视为一致。重新生成：`python3 scripts/unbiasedness_report.py`。" % SEED,
        "",
    ]
    out += section_uniform()
    out += section_weighted()
    out += section_weighted_k2()
    out += section_stratified()
    out += section_stream()
    out += section_stream_weighted()
    path = Path(__file__).resolve().parent.parent / "docs" / "UNBIASEDNESS.md"
    path.write_text("\n".join(out), encoding="utf-8")
    print("written: %s" % path)


if __name__ == "__main__":
    main()
