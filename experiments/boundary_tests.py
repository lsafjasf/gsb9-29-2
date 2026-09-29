#!/usr/bin/env python3
"""边界实验：random.choices 在异常/极端输入下的行为。

被测对象: CPython 3.12.3 标准库 random.Random.choices
          (/usr/lib/python3.12/random.py:454)
运行方式: python3 experiments/boundary_tests.py
仅使用标准库；固定种子 SEED=20260930，结果可完整复现。
"""

import random

SEED = 20260930
results = []


def record(case, expect, fn):
    """运行 fn，记录实际行为（返回值摘要或异常），与期望对照。"""
    try:
        out = fn()
        actual = f"返回 {out!r}"
        kind = "return"
    except Exception as e:
        actual = f"{type(e).__name__}: {e}"
        kind = "raise"
    ok = "✓" if expect in actual else "✗"
    results.append((case, expect, actual, ok))
    print(f"[{ok}] {case}")
    print(f"    期望: {expect}")
    print(f"    实际: {actual}")


def main():
    rng = random.Random(SEED)

    print("--- B. 边界输入 ---")
    record("B1 权重全为零 [0,0,0]",
           "ValueError: Total of weights must be greater than zero",
           lambda: rng.choices(list("ABC"), weights=[0, 0, 0], k=1))

    record("B2 单元素 + 正权重，抽 1000 次恒为该元素",
           "1000",
           lambda: sum(1 for x in rng.choices(["only"], weights=[7], k=1000)
                       if x == "only"))

    record("B3 单元素 + 零权重",
           "ValueError: Total of weights must be greater than zero",
           lambda: rng.choices(["only"], weights=[0], k=1))

    record("B4 权重相差百万倍 [1e6,1,1] 可正常运行",
           "返回",
           lambda: rng.choices(list("ABC"), weights=[1_000_000, 1, 1], k=3))

    record("B5a 流式/长度未知：生成器作为总体",
           "TypeError: object of type 'generator' has no len()",
           lambda: rng.choices((x for x in [1, 2, 3]), weights=[1, 1, 1], k=1))

    record("B5b 流式/长度未知：迭代器作为总体",
           "TypeError: object of type 'list_iterator' has no len()",
           lambda: rng.choices(iter([1, 2, 3]), weights=[1, 1, 1], k=1))

    record("B5c 流式/长度未知：map 对象作为总体",
           "TypeError: object of type 'map' has no len()",
           lambda: rng.choices(map(str, [1, 2, 3]), weights=[1, 1, 1], k=1))

    record("B6 空总体",
           "IndexError: list index out of range",
           lambda: rng.choices([], k=1))

    record("B7a 权重含 inf",
           "ValueError: Total of weights must be finite",
           lambda: rng.choices(list("AB"), weights=[1.0, float("inf")], k=1))

    record("B7b 权重含 nan",
           "ValueError: Total of weights must be finite",
           lambda: rng.choices(list("AB"), weights=[1.0, float("nan")], k=1))

    record("B7c 权重和溢出为 inf（1e308+1e308）",
           "ValueError: Total of weights must be finite",
           lambda: rng.choices(list("AB"), weights=[1e308, 1e308], k=1))

    record("B8 k=0 返回空列表",
           "返回 []",
           lambda: rng.choices(list("AB"), weights=[1, 1], k=0))

    record("B9 旧式调用：第二个位置参数传 int",
           "TypeError: The number of choices must be a keyword argument",
           lambda: rng.choices(list("AB"), 3))

    record("B10 权重个数与总体长度不一致",
           "ValueError: The number of weights does not match the population",
           lambda: rng.choices(list("ABC"), weights=[1, 2], k=1))

    record("B11 同时指定 weights 与 cum_weights",
           "TypeError: Cannot specify both weights and cumulative weights",
           lambda: rng.choices(list("AB"), weights=[1, 1], cum_weights=[1, 2], k=1))

    record("B12 全负权重（总和<0）",
           "ValueError: Total of weights must be greater than zero",
           lambda: rng.choices(list("AB"), weights=[-5, -5], k=1))

    record("B13 负权重 [5,-1,5]：不报错（静默偏差，见频率实验用例5）",
           "返回",
           lambda: rng.choices(list("ABC"), weights=[5, -1, 5], k=3))

    record("B14 巨型整数权重 10**309（总和转 float 溢出）",
           "OverflowError: int too large to convert to float",
           lambda: rng.choices(["a"], weights=[10 ** 309], k=1))

    record("B15 用户自供非单调 cum_weights [5,4,9]：不报错（静默偏差）",
           "返回",
           lambda: rng.choices(list("ABC"), cum_weights=[5, 4, 9], k=3))

    record("B16 权重本身可以是迭代器（总体却必须有 len）",
           "返回",
           lambda: rng.choices(list("AB"), weights=(x for x in [1, 2]), k=2))

    record("B17 range 等有 len 的序列可作为总体",
           "返回",
           lambda: rng.choices(range(3), weights=[1, 1, 1], k=2))

    print("\n--- 汇总 ---")
    n_ok = sum(1 for r in results if r[3] == "✓")
    print(f"{n_ok}/{len(results)} 项行为与预期一致")
    print("\n关键观察:")
    print("  * B1/B3/B7/B10/B12 为显式校验（ValueError/TypeError），行为清晰。")
    print("  * B5 流式输入不支持：L462 len(population) 要求总体有界且已知长度。")
    print("  * B6 空总体抛 IndexError 而非 ValueError，异常类型泄漏实现细节。")
    print("  * B13/B15 负权重/非单调 cum_weights 不报错，产生静默错误分布。")
    print("  * B14 巨型 int 权重触发未文档化的 OverflowError（L481 '+ 0.0' 转换）。")


if __name__ == "__main__":
    main()
