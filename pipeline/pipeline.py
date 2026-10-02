"""管线编排：顺序驱动阶段，阶段失败时回滚到最近的完好检查点。"""
from .stages.decode import DecodeStage
from .stages.color import ColorStage
from .stages.geometry import GeometryStage
from .stages.filter import FilterStage
from .stages.encode import EncodeStage
from .buffers import BufferPool


class PipelineError(Exception):
    """某阶段失败；checkpoint 是失败阶段的输入（最近一个完好状态）。"""

    def __init__(self, stage_index, stage_name, checkpoint, original):
        super().__init__("stage %d (%s) failed: %s" % (stage_index, stage_name, original))
        self.stage_index = stage_index
        self.stage_name = stage_name
        self.checkpoint = checkpoint
        self.original = original


class Pipeline:
    """阶段清单在构造时固定，运行时按顺序调用 stage.run(item, pool)。

    每个阶段都是一个事务：成功则返回新 item；失败则抛出，输入 item 不被修改，
    编排器把该 item 作为 checkpoint 放进 PipelineError，供调用方回滚/重试。
    """

    def __init__(self, stages, pool=None):
        self.stages = list(stages)
        self.pool = pool if pool is not None else BufferPool()

    def run(self, item):
        for index, stage in enumerate(self.stages):
            try:
                item = stage.run(item, self.pool)
            except Exception as exc:
                if isinstance(exc, PipelineError):
                    raise
                raise PipelineError(index, stage.name, item, exc) from exc
        return item


def build_pipeline(config=None, pool=None):
    """按配置组装管线。config 中缺省或为 None 的阶段被显式跳过（分支）。"""
    config = config or {}
    stages = [DecodeStage()]
    if config.get("color"):
        stages.append(ColorStage(config["color"]))
    if config.get("geometry"):
        stages.append(GeometryStage(config["geometry"]))
    if config.get("filter"):
        stages.append(FilterStage(config["filter"]))
    stages.append(EncodeStage())
    return Pipeline(stages, pool=pool)
