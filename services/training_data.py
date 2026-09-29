"""训练数据模块 —— 重构后：分层抽样委托给统一组件。

参数语义对齐：组件统一使用条数 k；本调用点保留对外的 frac 比例语义，
在边界处换算 k = round(frac * n) 后委托组件。
随机源由裸 rand_fn 改为 FuncSource 适配注入。
有效输入范围内与旧实现逐位等价（见 tests/test_differential.py）。
"""

from sampling import Sampler, StratifiedSampling


def sample_stratified(records, key_fn, frac, rng):
    k = int(round(frac * len(records)))
    return Sampler(StratifiedSampling(key_fn), rng).sample(records, k)
