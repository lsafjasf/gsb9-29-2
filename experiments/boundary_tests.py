#!/usr/bin/env python3
"""边界实验：random.choices 在极端输入下的行为。

被测对象: CPython 3.12.3 标准库 random.Random.choices
          (/usr/lib/python3.12/random.py:454)
运行方式: python3 experiments/boundary_tests.py
"""

import random
import tracemalloc

SEED = 20260930


def probe(title, fn):
    """执行 fn，打印返回值或异常类型与信息。"""
    try:
        result = fn()
    except Exception as exc:
        print(f"[{title}] 异常 {type(exc).__name__}: {exc}")
        return None
    print(f"[{title}] 正常返回: {result!r}")
    return result


def main():
    rng = random.Random(SEED)

    print("--- 1. 权重全为零 ---")
    probe("weights=[0,0,0]", lambda: rng.choices(list("abc"), weights=[0, 0, 0], k=1))
    probe("weights=[0.0,0.0]", lambda: rng.choices(list("ab"), weights=[0.0, 0.0], k=1))
    probe("cum_weights=[0,0,0]", lambda: rng.choices(list("abc"), cum_weights=[0, 0, 0], k=1))

    print("\n--- 2. 单个元素 ---")
    probe("单元素 weights=[7]", lambda: rng.choices(["only"], weights=[7], k=5))
    probe("单元素 weights=[0]", lambda: rng.choices(["only"], weights=[0], k=1))
    probe("单元素 weights=None", lambda: rng.choices(["only"], k=5))

    print("\n--- 3. 权重相差百万倍 ---")
    out = probe("weights=[1, 1e6] k=10", lambda: rng.choices(["tiny", "big"], weights=[1, 1_000_000], k=10))
    probe("weights=[1e-6, 1.0] k=10", lambda: rng.choices(["tiny", "big"], weights=[1e-6, 1.0], k=10))

    print("\n--- 4. 流式 / 长度未知 ---")
    probe("生成器(无 len)", lambda: rng.choices((x for x in "abc"), weights=[1, 2, 3], k=1))
    probe("迭代器 iter(list)", lambda: rng.choices(iter(["a", "b", "c"]), weights=[1, 2, 3], k=1))
    probe("map 对象", lambda: rng.choices(map(str, [1, 2, 3]), weights=[1, 2, 3], k=1))
    probe("range(支持序列协议)", lambda: rng.choices(range(3), weights=[1, 2, 3], k=3))

    print("\n--- 5. 其他防御性边界 ---")
    probe("空总体 weights=None", lambda: rng.choices([], k=1))
    probe("空总体 weights=[]", lambda: rng.choices([], weights=[], k=1))
    probe("weights 与 cum_weights 同时给", lambda: rng.choices(list("ab"), weights=[1, 2], cum_weights=[1, 3]))
    probe("权重个数与总体不等长", lambda: rng.choices(list("abc"), weights=[1, 2]))
    probe("权重含 inf -> 总和 inf", lambda: rng.choices(list("ab"), weights=[1.0, float("inf")]))
    probe("权重含 nan", lambda: rng.choices(list("ab"), weights=[1.0, float("nan")]))
    probe("权重溢出 1e308+1e308", lambda: rng.choices(list("ab"), weights=[1e308, 1e308]))
    probe("k=0", lambda: rng.choices(list("ab"), weights=[1, 2], k=0))
    probe("weights 位置传 int(旧式调用)", lambda: rng.choices(list("ab"), 5))

    print("\n--- 6. 内存占用（tracemalloc 实测）---")
    for n in (100_000, 1_000_000, 5_000_000):
        population = list(range(n))
        weights = [1] * n
        r = random.Random(SEED)
        tracemalloc.start()
        r.choices(population, weights=weights, k=1)
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(f"n={n:>9,}  单次 choices(k=1) 峰值内存 = {peak / 2**20:8.2f} MiB"
              f"  (约 {peak / n:6.1f} 字节/元素, 来自 L469 的 cum_weights 列表)")


if __name__ == "__main__":
    main()
