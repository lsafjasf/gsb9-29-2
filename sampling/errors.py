"""统一抽样组件的异常体系。"""


class SamplingError(Exception):
    """所有抽样相关错误的基类。"""


class SampleSizeError(SamplingError, ValueError):
    """样本量非法（负数，或大于总体量）。"""


class WeightError(SamplingError, ValueError):
    """权重非法（负数、NaN/Inf、长度不匹配、全为零）。"""


class RandomSourceUnavailable(SamplingError, RuntimeError):
    """随机源未注入、接口不完整或产出越界。"""
