"""流式抽样：输入为迭代器（无 len、只能单遍）时使用。

与批量策略的语义差异（受流式限制，文档化约定）：
    k 大于实际总体量时不报错，返回全部可用条目（clamp）。
"""

import heapq
import math

from .errors import SampleSizeError, WeightError, RandomSourceUnavailable
from .strategies import largest_remainder, _uniform_pick


def _check_stream_k(k):
    if not isinstance(k, int) or isinstance(k, bool) or k < 0:
        raise SampleSizeError("样本量 k 必须是非负 int，得到 %r" % (k,))


def reservoir_uniform(iterable, k, rng):
    """等概率水塘抽样（Algorithm R），O(k) 内存。"""
    _check_stream_k(k)
    if k == 0:
        return []
    reservoir = []
    it = iter(iterable)
    for _ in range(k):
        try:
            reservoir.append(next(it))
        except StopIteration:
            return reservoir  # 总体不足 k：返回全部
    for i, item in enumerate(it, start=k):
        j = rng.randbelow(i + 1)
        if j < k:
            reservoir[j] = item
    return reservoir


def reservoir_weighted(pairs, k, rng):
    """按权重水塘抽样（A-Res），pairs 为 (item, weight) 迭代器。

    键值 key = u^(1/w)，取最大的 k 个；权重为零的条目 key 为 0，
    只有在正权重条目不足 k 个时才可能进入结果。
    """
    _check_stream_k(k)
    if k == 0:
        return []
    heap = []  # (key, 序号, item) 的小顶堆，保留 key 最大的 k 个
    for seq, (item, w) in enumerate(pairs):
        if not isinstance(w, (int, float)) or isinstance(w, bool) \
                or math.isnan(w) or math.isinf(w) or w < 0:
            raise WeightError("非法权重：%r（需为非负有限实数）" % (w,))
        if w == 0:
            key = 0.0
        else:
            u = 1.0 - rng.random()  # (0, 1]，避免 log(0)
            if not (0.0 < u <= 1.0):
                raise RandomSourceUnavailable(
                    "随机源 random() 返回值越界（应在 [0.0, 1.0)）")
            key = math.exp(math.log(u) / w)
        if len(heap) < k:
            heapq.heappush(heap, (key, seq, item))
        elif key > heap[0][0]:
            heapq.heapreplace(heap, (key, seq, item))
    return [item for _, _, item in heap]


def reservoir_stratified(iterable, key_fn, k, rng):
    """分层流式抽样：单遍扫描，每层维护大小为 k 的水塘，
    结束时按最终层大小用最大余数法分配名额，再从各层水塘中等概率子抽样。

    均匀性：大小为 k 的均匀水塘中再均匀取 a 条，等价于直接从层内均匀取 a 条。
    """
    _check_stream_k(k)
    if k == 0:
        return []
    reservoirs = {}
    counts = {}
    for item in iterable:
        key = key_fn(item)
        c = counts.get(key, 0)
        counts[key] = c + 1
        res = reservoirs.setdefault(key, [])
        if len(res) < k:
            res.append(item)
        else:
            j = rng.randbelow(c + 1)
            if j < k:
                res[j] = item
    total = sum(counts.values())
    if total <= k:  # 总体不足 k：返回全部
        return [item for res in reservoirs.values() for item in res]
    alloc = largest_remainder(list(counts.values()), k)
    out = []
    for res, a in zip(reservoirs.values(), alloc):
        out.extend(_uniform_pick(res, a, rng))
    return out
