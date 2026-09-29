"""重构后：显式状态机解析器。

协议帧格式：
    0xAA TYPE LEN [EXT_HI EXT_LO] PAYLOAD... CHECKSUM
    - LEN 0x00..0xFE : 短长度
    - LEN 0xFF       : 扩展长度，后跟 2 字节大端长度，> MAX_EXT_LENGTH 报错
    - CHECKSUM       : TYPE、长度字节、PAYLOAD 的 XOR

设计要点：
    - 状态 / 事件为显式枚举，迁移全部集中在 TRANSITIONS 表。
    - 未定义的 (state, event) 组合抛出 UndefinedTransitionError，绝不悄悄容忍。
    - 每个状态可产生的事件集合在 STATE_EVENTS 中声明，测试据此校验
      迁移表既不多也不少。
    - 新增消息类型的唯一改动点：MESSAGE_TYPES 字典（见下）。
"""

from collections import Counter
from enum import Enum, auto

MAGIC = 0xAA
MAX_EXT_LENGTH = 1024

# === 新增消息类型的唯一改动位置 ===
# 分类器（KNOWN_TYPE）与消息输出都从这里取，无需改任何分支逻辑。
MESSAGE_TYPES = {
    0x01: "PING",
    0x02: "PONG",
    0x03: "DATA",
    0x04: "ACK",
}


class State(Enum):
    WAIT_MAGIC = auto()
    READ_TYPE = auto()
    READ_LEN = auto()
    READ_EXT_HI = auto()
    READ_EXT_LO = auto()
    READ_PAYLOAD = auto()
    READ_CHECKSUM = auto()
    RESYNC = auto()


class Event(Enum):
    MAGIC = auto()            # 字节 == 0xAA
    OTHER_BYTE = auto()       # 字节 != 0xAA（在 WAIT_MAGIC / RESYNC 中）
    KNOWN_TYPE = auto()       # 类型字节已注册
    UNKNOWN_TYPE = auto()     # 类型字节未注册
    LEN_ZERO = auto()         # LEN == 0
    LEN_SHORT = auto()        # 1 <= LEN <= 0xFE
    LEN_EXT = auto()          # LEN == 0xFF
    BYTE = auto()             # 普通字节（EXT_HI）
    EXT_LEN_ZERO = auto()     # 扩展长度 == 0
    EXT_LEN_OK = auto()       # 0 < 扩展长度 <= MAX_EXT_LENGTH
    EXT_LEN_TOO_LARGE = auto()
    PAYLOAD_BYTE = auto()     # 负载字节，且不是最后一个
    PAYLOAD_LAST = auto()     # 最后一个负载字节
    CHECKSUM_OK = auto()
    CHECKSUM_BAD = auto()
    EOF = auto()              # 输入结束


class UndefinedTransitionError(RuntimeError):
    """(state, event) 组合未在 TRANSITIONS 中定义时抛出。"""


# 每个状态可产生的事件全集（分类器只能产出这些；迁移表必须恰好覆盖）。
STATE_EVENTS = {
    State.WAIT_MAGIC: frozenset({Event.MAGIC, Event.OTHER_BYTE, Event.EOF}),
    State.READ_TYPE: frozenset({Event.KNOWN_TYPE, Event.UNKNOWN_TYPE, Event.EOF}),
    State.READ_LEN: frozenset({Event.LEN_ZERO, Event.LEN_SHORT, Event.LEN_EXT, Event.EOF}),
    State.READ_EXT_HI: frozenset({Event.BYTE, Event.EOF}),
    State.READ_EXT_LO: frozenset(
        {Event.EXT_LEN_ZERO, Event.EXT_LEN_OK, Event.EXT_LEN_TOO_LARGE, Event.EOF}
    ),
    State.READ_PAYLOAD: frozenset({Event.PAYLOAD_BYTE, Event.PAYLOAD_LAST, Event.EOF}),
    State.READ_CHECKSUM: frozenset({Event.CHECKSUM_OK, Event.CHECKSUM_BAD, Event.EOF}),
    State.RESYNC: frozenset({Event.MAGIC, Event.OTHER_BYTE, Event.EOF}),
}

# 完整迁移表：(state, event) -> (next_state, action_name)
TRANSITIONS = {
    (State.WAIT_MAGIC, Event.MAGIC): (State.READ_TYPE, "_on_magic"),
    (State.WAIT_MAGIC, Event.OTHER_BYTE): (State.WAIT_MAGIC, "_on_bad_magic"),
    (State.WAIT_MAGIC, Event.EOF): (State.WAIT_MAGIC, "_on_noop"),

    (State.READ_TYPE, Event.KNOWN_TYPE): (State.READ_LEN, "_on_known_type"),
    (State.READ_TYPE, Event.UNKNOWN_TYPE): (State.RESYNC, "_on_unknown_type"),
    (State.READ_TYPE, Event.EOF): (State.WAIT_MAGIC, "_on_truncated"),

    (State.READ_LEN, Event.LEN_ZERO): (State.READ_CHECKSUM, "_on_len"),
    (State.READ_LEN, Event.LEN_SHORT): (State.READ_PAYLOAD, "_on_len"),
    (State.READ_LEN, Event.LEN_EXT): (State.READ_EXT_HI, "_on_len"),
    (State.READ_LEN, Event.EOF): (State.WAIT_MAGIC, "_on_truncated"),

    (State.READ_EXT_HI, Event.BYTE): (State.READ_EXT_LO, "_on_ext_hi"),
    (State.READ_EXT_HI, Event.EOF): (State.WAIT_MAGIC, "_on_truncated"),

    (State.READ_EXT_LO, Event.EXT_LEN_ZERO): (State.READ_CHECKSUM, "_on_ext_lo_ok"),
    (State.READ_EXT_LO, Event.EXT_LEN_OK): (State.READ_PAYLOAD, "_on_ext_lo_ok"),
    (State.READ_EXT_LO, Event.EXT_LEN_TOO_LARGE): (State.RESYNC, "_on_ext_lo_too_large"),
    (State.READ_EXT_LO, Event.EOF): (State.WAIT_MAGIC, "_on_truncated"),

    (State.READ_PAYLOAD, Event.PAYLOAD_BYTE): (State.READ_PAYLOAD, "_on_payload"),
    (State.READ_PAYLOAD, Event.PAYLOAD_LAST): (State.READ_CHECKSUM, "_on_payload"),
    (State.READ_PAYLOAD, Event.EOF): (State.WAIT_MAGIC, "_on_truncated"),

    (State.READ_CHECKSUM, Event.CHECKSUM_OK): (State.WAIT_MAGIC, "_on_checksum_ok"),
    (State.READ_CHECKSUM, Event.CHECKSUM_BAD): (State.RESYNC, "_on_checksum_bad"),
    (State.READ_CHECKSUM, Event.EOF): (State.WAIT_MAGIC, "_on_truncated"),

    (State.RESYNC, Event.MAGIC): (State.READ_TYPE, "_on_magic"),
    (State.RESYNC, Event.OTHER_BYTE): (State.RESYNC, "_on_noop"),
    (State.RESYNC, Event.EOF): (State.RESYNC, "_on_noop"),
}


class Parser:
    """增量式解析器：feed(bytes) 任意分片喂入，eof() 结束。"""

    def __init__(self):
        self.state = State.WAIT_MAGIC
        self.events = []                 # 输出: ("message", name, payload) / ("error", code, offset)
        self.offset = 0                  # 已消费字节数（即下一个字节的偏移）
        self.transitions = []            # 迁移序列: (state, event, next_state)
        self.coverage = Counter()        # (state, event) -> 次数
        self._eof = False
        self._reset_frame()

    def _reset_frame(self):
        self._type = 0
        self._length = 0
        self._remaining = 0
        self._ext_hi = 0
        self._payload = bytearray()
        self._checksum = 0

    # ---- 公共 API ----

    def feed(self, data):
        if self._eof:
            raise RuntimeError("feed() after eof()")
        for byte in data:
            event = self._classify(byte)
            self._step(event, byte)
            self.offset += 1

    def eof(self):
        self._eof = True
        self._step(Event.EOF, None)

    # ---- 状态机核心 ----

    def _classify(self, byte):
        """把 (当前状态, 字节) 归类为事件。只允许产出 STATE_EVENTS 声明的事件。"""
        st = self.state
        if st is State.WAIT_MAGIC or st is State.RESYNC:
            event = Event.MAGIC if byte == MAGIC else Event.OTHER_BYTE
        elif st is State.READ_TYPE:
            event = Event.KNOWN_TYPE if byte in MESSAGE_TYPES else Event.UNKNOWN_TYPE
        elif st is State.READ_LEN:
            if byte == 0xFF:
                event = Event.LEN_EXT
            elif byte == 0x00:
                event = Event.LEN_ZERO
            else:
                event = Event.LEN_SHORT
        elif st is State.READ_EXT_HI:
            event = Event.BYTE
        elif st is State.READ_EXT_LO:
            length = (self._ext_hi << 8) | byte
            if length > MAX_EXT_LENGTH:
                event = Event.EXT_LEN_TOO_LARGE
            elif length == 0:
                event = Event.EXT_LEN_ZERO
            else:
                event = Event.EXT_LEN_OK
        elif st is State.READ_PAYLOAD:
            event = Event.PAYLOAD_LAST if self._remaining == 1 else Event.PAYLOAD_BYTE
        elif st is State.READ_CHECKSUM:
            event = Event.CHECKSUM_OK if byte == self._checksum else Event.CHECKSUM_BAD
        else:  # pragma: no cover - 防御未知状态
            raise UndefinedTransitionError(f"no classifier for state {st}")
        if event not in STATE_EVENTS[st]:
            raise UndefinedTransitionError(f"classifier produced {event} in {st}")
        return event

    def _step(self, event, byte):
        key = (self.state, event)
        if key not in TRANSITIONS:
            # 未定义组合：显式报错，绝不悄悄忽略。
            raise UndefinedTransitionError(
                f"undefined transition: state={self.state.name} event={event.name}"
            )
        next_state, action_name = TRANSITIONS[key]
        getattr(self, action_name)(byte)
        self.transitions.append((self.state, event, next_state))
        self.coverage[key] += 1
        self.state = next_state

    # ---- 动作 ----

    def _on_magic(self, byte):
        self._reset_frame()

    def _on_bad_magic(self, byte):
        self.events.append(("error", "bad_magic", self.offset))

    def _on_known_type(self, byte):
        self._type = byte
        self._checksum ^= byte

    def _on_unknown_type(self, byte):
        self.events.append(("error", "unknown_type", self.offset))

    def _on_len(self, byte):
        self._checksum ^= byte
        if byte != 0xFF:
            self._length = byte
            self._remaining = byte

    def _on_ext_hi(self, byte):
        self._checksum ^= byte
        self._ext_hi = byte

    def _on_ext_lo_ok(self, byte):
        self._checksum ^= byte
        self._length = (self._ext_hi << 8) | byte
        self._remaining = self._length

    def _on_ext_lo_too_large(self, byte):
        self.events.append(("error", "length_too_large", self.offset))

    def _on_payload(self, byte):
        self._payload.append(byte)
        self._checksum ^= byte
        self._remaining -= 1

    def _on_checksum_ok(self, byte):
        self.events.append(("message", MESSAGE_TYPES[self._type], bytes(self._payload)))

    def _on_checksum_bad(self, byte):
        self.events.append(("error", "checksum_mismatch", self.offset))

    def _on_truncated(self, byte):
        self.events.append(("error", "truncated", self.offset))

    def _on_noop(self, byte):
        pass
