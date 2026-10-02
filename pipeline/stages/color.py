"""色彩阶段：invert / grayscale / sepia，均为逐像素映射，就地（in-place）完成。

逐像素映射在参数校验后没有失败路径，因此就地写不破坏回滚语义。
"""
_INV_TABLE = bytes(range(255, -1, -1))


def _op_invert(buf, _op):
    n = buf.nbytes
    buf.data[:n] = buf.data[:n].translate(_INV_TABLE)


def _op_grayscale(buf, _op):
    px = buf.data
    for i in range(0, buf.nbytes, 3):
        y = (px[i] * 299 + px[i + 1] * 587 + px[i + 2] * 114) // 1000
        px[i] = px[i + 1] = px[i + 2] = y


def _op_sepia(buf, _op):
    px = buf.data
    for i in range(0, buf.nbytes, 3):
        r = px[i]
        g = px[i + 1]
        b = px[i + 2]
        tr = (r * 393 + g * 769 + b * 189) // 1000
        tg = (r * 349 + g * 686 + b * 168) // 1000
        tb = (r * 272 + g * 534 + b * 131) // 1000
        px[i] = 255 if tr > 255 else tr
        px[i + 1] = 255 if tg > 255 else tg
        px[i + 2] = 255 if tb > 255 else tb


_OPS = {
    "invert": _op_invert,
    "grayscale": _op_grayscale,
    "sepia": _op_sepia,
}


class ColorStage:
    name = "color"

    def __init__(self, ops):
        for op in ops:
            if op.get("name") not in _OPS:
                raise ValueError("unknown color op: %r" % (op.get("name"),))
        self.ops = list(ops)

    def run(self, buf, pool):
        for op in self.ops:
            _OPS[op["name"]](buf, op)
            buf.metadata["ops"].append(op["name"])
        return buf
