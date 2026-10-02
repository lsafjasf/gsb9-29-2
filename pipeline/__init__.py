"""显式图像处理管线。

阶段固定为五类：解码 -> 色彩 -> 几何 -> 滤波 -> 编码。
阶段之间只通过 ImageBuffer（或首尾的 bytes）交接，不存在共享隐藏状态。
"""
from .buffers import ImageBuffer, BufferPool
from .pipeline import Pipeline, PipelineError, build_pipeline

__all__ = [
    "ImageBuffer",
    "BufferPool",
    "Pipeline",
    "PipelineError",
    "build_pipeline",
]
