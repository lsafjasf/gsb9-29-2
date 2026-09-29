"""重构前的遗留解析器（冻结，作为差分测试的行为基准）。

典型问题：一堆布尔标志（_started/_got_type/_got_len/_ext/_resync ...）
加上 while 循环里的早退 continue，状态组合隐式且互相牵连。
不要修改本文件；新代码见 fsm_parser.py。
"""

MAGIC = 0xAA
TYPES = {0x01: "PING", 0x02: "PONG", 0x03: "DATA", 0x04: "ACK"}
MAX_EXT = 1024


class LegacyParser:
    def __init__(self):
        self.events = []
        self._buf = bytearray()
        self._pos = 0
        self._eof = False
        self._reset()

    def _reset(self):
        self._started = False
        self._resync = False
        self._got_type = False
        self._got_len = False
        self._ext = False
        self._got_ext_hi = False
        self._type = 0
        self._length = 0
        self._remaining = 0
        self._payload = bytearray()
        self._cksum = 0

    def _start_frame(self):
        self._started = True
        self._resync = False
        self._got_type = False
        self._got_len = False
        self._ext = False
        self._got_ext_hi = False
        self._type = 0
        self._length = 0
        self._remaining = 0
        self._payload = bytearray()
        self._cksum = 0

    def feed(self, data):
        if self._eof:
            raise RuntimeError("feed() after eof()")
        self._buf.extend(data)
        self._drain()

    def eof(self):
        self._eof = True
        if self._started and not self._resync:
            self.events.append(("error", "truncated", self._pos))

    def _drain(self):
        while self._buf:
            b = self._buf[0]

            if self._resync:
                del self._buf[0]
                self._pos += 1
                if b == MAGIC:
                    self._start_frame()
                continue

            if not self._started:
                del self._buf[0]
                if b != MAGIC:
                    self.events.append(("error", "bad_magic", self._pos))
                    self._pos += 1
                    continue
                self._pos += 1
                self._start_frame()
                continue

            if not self._got_type:
                del self._buf[0]
                if b not in TYPES:
                    self.events.append(("error", "unknown_type", self._pos))
                    self._pos += 1
                    self._started = False
                    self._resync = True
                    continue
                self._type = b
                self._cksum ^= b
                self._got_type = True
                self._pos += 1
                continue

            if not self._got_len:
                del self._buf[0]
                self._cksum ^= b
                if self._ext:
                    if not self._got_ext_hi:
                        self._length = b << 8
                        self._got_ext_hi = True
                        self._pos += 1
                        continue
                    self._length |= b
                    if self._length > MAX_EXT:
                        self.events.append(("error", "length_too_large", self._pos))
                        self._pos += 1
                        self._started = False
                        self._resync = True
                        continue
                    self._remaining = self._length
                    self._got_len = True
                    self._pos += 1
                    continue
                if b == 0xFF:
                    self._ext = True
                    self._pos += 1
                    continue
                self._length = b
                self._remaining = b
                self._got_len = True
                self._pos += 1
                continue

            if self._remaining > 0:
                del self._buf[0]
                self._payload.append(b)
                self._cksum ^= b
                self._remaining -= 1
                self._pos += 1
                continue

            del self._buf[0]
            if b != self._cksum:
                self.events.append(("error", "checksum_mismatch", self._pos))
                self._pos += 1
                self._started = False
                self._resync = True
                continue
            self.events.append(("message", TYPES[self._type], bytes(self._payload)))
            self._pos += 1
            self._reset()
