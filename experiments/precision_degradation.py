#!/usr/bin/env python3
"""精度退化实验：float64 前缀和舍入如何改变 random.choices 的真实分布。

被测对象: CPython 3.12.3 标准库 random.Random.choices
          (/usr/lib/python3.12/random.py:454)
运行方式: python3 experiments/precision_degradation.py
仅使用标准库；不依赖随机抽样，全部用精确有理数（Fraction）给出决定性证据。

机理:
  L469  cum_weights = list(_accumulate(weights))  # float 权重逐步累加，逐步舍入
  L481  total = cum_weights[-1] + 0.0             # 总和强制转 float64
  L488  bisect(cum_weights, random() * total)     # 区间宽度 = 相邻累计值之差
  当某个权重小于当前累计和的 ulp（约 2^53 倍动态范围）时，
  累加结果被舍入，区间宽度变为 0 或被邻居吞并，概率被精确改写。
"""

from fractions import Fraction
from itertools import accumulate


def implied_probabilities(cum_weights):
    """由 float 化后的 cum_weights 精确推导出各元素的真实选中概率。

    对单调 cum（float 权重累加结果必然单调），下标 i 的命中测度为
    (cum[i] - cum[i-1]) / total，全部用 Fraction 精确计算。
    """
    cum = [Fraction(float(c)) for c in cum_weights]
    total = Fraction(float(cum_weights[-1]) + 0.0)
    probs, prev = [], Fraction(0)
    for c in cum:
        probs.append((c - prev) / total)
        prev = c
    return probs


def fair_probabilities(weights):
    total = Fraction(sum(Fraction(w) for w in weights))
    return [Fraction(w) / total for w in weights]


def show_case(title, weights):
    cum = list(accumulate(weights))
    implied = implied_probabilities(cum)
    fair = fair_probabilities(weights)
    print(f"\n=== {title} ===")
    print(f"weights = {weights}")
    print(f"float 化 cum_weights = {[float(c) for c in cum]}")
    print(f"{'元素':>4} {'权重':>22} {'文档承诺概率 w/Σw':>22} "
          f"{'实现隐含真实概率':>22} {'相对误差':>12}")
    for i, (w, f, p) in enumerate(zip(weights, fair, implied)):
        rel = "N/A" if f == 0 else f"{float((p - f) / f) * 100:+.1f}%"
        print(f"{i:>4} {str(w):>22} {float(f):>22.6e} {float(p):>22.6e} {rel:>12}")
    zeroed = [i for i, (f, p) in enumerate(zip(fair, implied)) if f > 0 and p == 0]
    if zeroed:
        print(f"!! 元素 {zeroed} 的选中概率被舍入为精确的 0（文档承诺值 > 0）")


def main():
    print("### D1. 浮点权重动态范围达到 2^53：小权重区间宽度被舍入为 0")
    show_case("D1: weights=[2**53, 1, 1]（浮点）",
              [2.0 ** 53, 1.0, 1.0])

    print("\n### D2. 概率不仅消失，还会被邻居吞并（重新分配）")
    show_case("D2: weights=[2**53, 3, 1]（浮点）",
              [2.0 ** 53, 3.0, 1.0])

    print("\n### D3. 退化阈值扫描：weights=[2**k, 1]（浮点），k=48..56")
    print(f"{'k':>4} {'cum_weights(float)':>44} {'小权重区间宽度':>14} {'状态':>8}")
    for k in range(48, 57):
        w = [2.0 ** k, 1.0]
        cum = list(accumulate(w))
        width = Fraction(float(cum[1])) - Fraction(float(cum[0]))
        status = "正常" if width > 0 else "概率=0"
        print(f"{k:>4} {str([float(c) for c in cum]):>44} "
              f"{float(width):>14.1f} {status:>8}")
    print("阈值恰为 2^53：与 float64 的 53 位有效位数（含隐含位）一致。")

    print("\n### D4. 整数权重：cum_weights 保持精确 int，但 L481 总和转 float 仍舍入")
    w = [10 ** 20, 1]
    cum = list(accumulate(w))
    total_float = cum[-1] + 0.0  # 与 L481 完全一致
    dead = Fraction(total_float) - Fraction(cum[-1])
    print(f"weights = [10**20, 1]")
    print(f"cum_weights(int, 精确) = {cum}")
    print(f"L481 total = cum[-1] + 0.0 = {total_float!r}")
    print(f"总和舍入误差 = float(Σw) - Σw = {dead} "
          f"(相对 {float(dead / Fraction(cum[-1])):.2e})")
    # 末尾元素的隐含概率 = 落在 [cum[-2], total) 内的测度 / total
    lo = Fraction(cum[-2])
    hi = min(Fraction(cum[-1]), Fraction(total_float))
    tail_p = max(Fraction(0), hi - lo) / Fraction(total_float)
    print(f"末尾元素(权重1)隐含概率 = {float(tail_p):.3e}，文档承诺 = {1 / (10**20 + 1):.3e}")
    print("float(Σw) < Σw 时末尾区间被截断（本例截断为精确的 0）；")
    print("float(Σw) > Σw 时多出的死区质量全部落在末尾元素（bisect hi=n-1 兜底）。")

    print("\n### D5. 巨型整数权重：L481 的 float 转换直接崩溃")
    try:
        import random
        random.Random(0).choices(["a"], weights=[10 ** 309], k=1)
    except OverflowError as e:
        print(f"weights=[10**309] -> OverflowError: {e}")
        print("正权重、总和有限（数学上），却因 float64 转换溢出而崩溃；")
        print("OverflowError 未被文档化，也未被实现捕获改写为 ValueError。")

    print("\n### D6. 负权重/非单调 cum_weights：无校验，静默改写分布")
    print("证据见 frequency_experiment.py 用例5 与 boundary_tests.py B13/B15。")
    print("L479-485 只校验『个数/总和>0/总和有限』，不校验单调性与非负性。")


if __name__ == "__main__":
    main()
