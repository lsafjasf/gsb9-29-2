"""失败样本归约（最小反例）。

在保持差异签名不变的前提下缩小输入：
1. ddmin 风格的分块删除；
2. 逐字节归零（常量归一化）。

归约后的样本仍然能复现同一类别、同一对错误、同一边成败的差异。
"""
from __future__ import annotations


def shrink(data, pred, budget=4000):
    """把 ``data`` 缩到仍满足 ``pred`` 的最小（近似）形式。"""
    data = bytes(data)
    if not pred(data):
        raise ValueError("predicate must hold on the original input")

    evals = 0

    def guarded(candidate):
        nonlocal evals
        evals += 1
        if evals > budget:
            return False
        return pred(candidate)

    data = _ddmin(data, guarded)
    data = _zero_pass(data, guarded)
    return data


def _ddmin(data, pred):
    n = 2
    while len(data) >= 2:
        chunk = (len(data) + n - 1) // n
        reduced = False
        for start in range(0, len(data), chunk):
            candidate = data[:start] + data[start + chunk :]
            if len(candidate) < len(data) and pred(candidate):
                data = candidate
                n = max(n - 1, 2)
                reduced = True
                break
        if not reduced:
            if n >= len(data):
                break
            n = min(n * 2, len(data))
    return data


def _zero_pass(data, pred):
    arr = bytearray(data)
    for i in range(len(arr)):
        if arr[i] == 0:
            continue
        original = arr[i]
        arr[i] = 0
        if not pred(bytes(arr)):
            arr[i] = original
    return bytes(arr)
