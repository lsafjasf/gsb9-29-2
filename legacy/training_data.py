"""训练数据模块 —— 重构前的原始实现。"""


def sample_stratified(records, key_fn, frac, rand_fn):
    """分层抽样。注意参数语义不同：frac 是抽样比例（0~1），不是条数。

    rand_fn 是裸的 () -> [0,1) 函数，索引用 int(rand_fn()*m) 推导。
    """
    groups = {}
    for rec in records:
        groups.setdefault(key_fn(rec), []).append(rec)
    n = len(records)
    k = int(round(frac * n))
    sizes = [len(g) for g in groups.values()]
    raw = [k * s / n for s in sizes]
    alloc = [int(x) for x in raw]
    leftover = k - sum(alloc)
    order = sorted(range(len(sizes)), key=lambda i: (-(raw[i] - alloc[i]), i))
    for i in order[:leftover]:
        alloc[i] += 1
    out = []
    for group, a in zip(groups.values(), alloc):
        idx = list(range(len(group)))
        for i in range(a):
            j = i + int(rand_fn() * (len(group) - i))
            idx[i], idx[j] = idx[j], idx[i]
        out.extend(group[t] for t in idx[:a])
    return out
