"""Pipeline：把五个阶段串起来，提供事务式的失败语义。"""

from .buffer import BufferPool
from .stages import (
    ColorStage,
    DecodeStage,
    EncodeStage,
    FilterStage,
    GeometryStage,
)


class PipelineError(Exception):
    """任一阶段失败时抛出，携带阶段名与原始异常。"""

    def __init__(self, stage, cause):
        super().__init__("stage %r failed: %s" % (stage, cause))
        self.stage = stage
        self.cause = cause


class Pipeline:
    """解码 -> 色彩 -> 几何 -> 滤波 -> 编码。

    失败回滚语义：
    - 输入 blob 是不可变 bytes，管线从不修改它，失败后可原样重试；
    - 任一阶段抛错时不产生任何输出（输出只在最后的编码阶段生成）；
    - finally 中把当前工作缓冲归还缓冲池，保证无泄漏（checked_out 归零）；
    - self.stages 是普通列表，可插入/替换自定义阶段。
    """

    def __init__(self, pool=None):
        self.pool = pool if pool is not None else BufferPool()
        self.decode = DecodeStage()
        self.encode = EncodeStage()
        self.stages = [ColorStage(), GeometryStage(), FilterStage()]

    def run(self, blob, ops):
        buf = None
        stage = self.decode
        try:
            buf = stage.apply_blob(blob, self.pool)
            for stage in self.stages:
                buf = stage.apply(buf, ops, self.pool)
            stage = self.encode
            return stage.apply_buf(buf)
        except Exception as exc:
            raise PipelineError(stage.name, exc) from exc
        finally:
            if buf is not None:
                self.pool.release(buf.data)
