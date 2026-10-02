"""阶段间交接的数据结构与缓冲池。

ImageBuffer 是阶段之间唯一的交接物：一块定长像素字节 + 形状 + 元数据。
BufferPool 复用已释放的 bytearray，使整管线存活缓冲有上界（见 README）。
"""


class ImageBuffer:
    __slots__ = ("data", "width", "height", "channels", "metadata")

    def __init__(self, data, width, height, channels=3, metadata=None):
        self.data = data          # bytearray；逻辑长度为 nbytes，可能因复用而更大
        self.width = width
        self.height = height
        self.channels = channels
        self.metadata = metadata if metadata is not None else {"ops": []}

    @property
    def nbytes(self):
        return self.width * self.height * self.channels


class BufferPool:
    """按容量复用 bytearray 的池，并统计在途缓冲，便于断言内存上界。"""

    def __init__(self):
        self._free = []
        self.live_bytes = 0
        self.max_live_bytes = 0

    def acquire(self, size):
        best = -1
        for idx, buf in enumerate(self._free):
            if len(buf) >= size and (best < 0 or len(buf) < len(self._free[best])):
                best = idx
        if best >= 0:
            buf = self._free.pop(best)
        else:
            buf = bytearray(size)
        self.live_bytes += len(buf)
        if self.live_bytes > self.max_live_bytes:
            self.max_live_bytes = self.live_bytes
        return buf

    def release(self, buf):
        if buf is not None:
            self.live_bytes -= len(buf)
            self._free.append(buf)

    def free_count(self):
        return len(self._free)

    def free_bytes(self):
        return sum(len(b) for b in self._free)
