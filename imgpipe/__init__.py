"""imgpipe：显式阶段的图像处理管线（仅标准库）。"""

from .buffer import BufferPool, ImageBuffer
from .pipeline import Pipeline, PipelineError

__all__ = ["BufferPool", "ImageBuffer", "Pipeline", "PipelineError"]
