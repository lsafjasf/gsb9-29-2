"""最小复现样本化简：ddmin 风格的块删除。

predicate(data) -> bool 为 True 表示样本仍触发目标行为（同一崩溃签名）。
"""


def minimize(data, predicate, max_predicate_calls=200):
    """在保持 predicate 为 True 的前提下尽量缩短 data。"""
    if not predicate(data):
        raise ValueError("initial sample does not satisfy predicate")
    calls = [1]

    def check(candidate):
        if calls[0] >= max_predicate_calls:
            return False
        calls[0] += 1
        return predicate(candidate)

    # 先尝试整体截断
    n = len(data)
    while n > 1:
        half = n // 2
        if check(data[:half]):
            data = data[:half]
            n = half
        else:
            break

    # 块删除，粒度由粗到细
    granularity = 2
    while len(data) >= 2 and granularity <= len(data):
        chunk = max(1, len(data) // granularity)
        reduced = False
        i = 0
        while i < len(data):
            candidate = data[:i] + data[i + chunk:]
            if candidate and check(candidate):
                data = candidate
                reduced = True
            else:
                i += chunk
        if not reduced:
            granularity *= 2
        elif granularity > 2:
            granularity = max(2, granularity // 2)
    return data
