"""阶段之间交接的唯一数据结构 ImageBuffer，以及复用 bytearray 的 BufferPool。"""

MAGIC = b"GSB1"

# 池中最多保留的缓冲数量，防止池本身无限膨胀（超出即丢弃，交给 GC）。
POOL_KEEP = 4


class ImageBuffer:
    """像素 + 尺寸 + 元数据。阶段只通过它交接，不共享任何隐藏状态。"""

    __slots__ = ("width", "height", "channels", "data", "meta")

    def __init__(self, width, height, channels, data, meta):
        self.width = width
        self.height = height
        self.channels = channels
        self.data = data  # bytearray，长度 == width * height * channels
        self.meta = meta  # dict，约定含 "history" 列表

    @property
    def nbytes(self):
        return self.width * self.height * self.channels


class BufferPool:
    """按精确尺寸复用 bytearray。

    - acquire(size)：池中有等长缓冲则复用，否则新建；
    - release(buf)：归还缓冲，池中数量超过 keep 时直接丢弃；
    - checked_out：当前被借出未归还的数量，测试用它验证失败时无泄漏。
    """

    def __init__(self, keep=POOL_KEEP):
        self._keep = keep
        self._free = []
        self.allocations = 0  # 真正新建 bytearray 的次数
        self.reuses = 0       # 命中复用的次数
        self.checked_out = 0

    def acquire(self, size):
        for i, candidate in enumerate(self._free):
            if len(candidate) == size:
                self._free.pop(i)
                self.reuses += 1
                self.checked_out += 1
                return candidate
        self.allocations += 1
        self.checked_out += 1
        return bytearray(size)

    def release(self, buf):
        if buf is None:
            return
        self.checked_out -= 1
        if len(self._free) < self._keep:
            self._free.append(buf)

    @property
    def available(self):
        return len(self._free)
