"""Explicit state-machine frame parser (post-refactor).

Behaviorally equivalent to legacy_parser.LegacyParser (proven by
tests/test_differential.py). All parsing decisions live in exactly two
places:

  * TRANSITIONS -- the full (state, event) -> (next state, action) table.
  * MESSAGE_TYPES -- the only place a message type is defined.

Frame layout (all multi-byte integers big-endian):
    0xAB 0xCD | TYPE(1) | LEN(2) | PAYLOAD(LEN) | CRC(1)
CRC = XOR of all bytes from TYPE through the end of PAYLOAD.

Adding a new message type: add ONE entry to MESSAGE_TYPES. Nothing else
(states, events, transitions, classifiers, actions) needs to change.

Any (state, event) pair not present in TRANSITIONS raises
UndefinedTransitionError -- undefined combinations are never tolerated
silently.
"""

from enum import Enum, auto

MAGIC1 = 0xAB
MAGIC2 = 0xCD
MAX_PAYLOAD = 1024


class State(Enum):
    SYNC1 = auto()    # hunting for the first magic byte
    SYNC2 = auto()    # saw 0xAB, expecting 0xCD
    TYPE = auto()     # expecting the type byte
    LEN_HI = auto()   # expecting the length high byte
    LEN_LO = auto()   # expecting the length low byte
    PAYLOAD = auto()  # consuming payload bytes
    CRC = auto()      # expecting the checksum byte


class Event(Enum):
    MAGIC1_BYTE = auto()    # byte == 0xAB
    MAGIC2_BYTE = auto()    # byte == 0xCD
    OTHER_BYTE = auto()     # any other byte while syncing
    KNOWN_TYPE = auto()     # type byte present in MESSAGE_TYPES
    UNKNOWN_TYPE = auto()   # type byte not in MESSAGE_TYPES
    ANY_BYTE = auto()       # unconditional byte (LEN_HI)
    LEN_OK = auto()         # length valid, payload follows
    LEN_ZERO = auto()       # length valid and zero, skip payload
    LEN_TOO_BIG = auto()    # length > MAX_PAYLOAD
    TYPE_LEN_BAD = auto()   # length violates the type-specific rule
    PAYLOAD_BYTE = auto()   # payload byte, more follow
    PAYLOAD_LAST = auto()   # last payload byte
    CRC_OK = auto()         # checksum matches
    CRC_BAD = auto()        # checksum mismatch


class UndefinedTransitionError(RuntimeError):
    """Raised for any (state, event) pair missing from TRANSITIONS."""


class MsgSpec:
    """Declares one message type. The single source of truth per type."""

    def __init__(self, name, exact_length=None):
        self.name = name
        self.exact_length = exact_length

    def length_error(self, length):
        if self.exact_length is not None and length != self.exact_length:
            return "message %s must have length %d, got %d" % (
                self.name, self.exact_length, length)
        return None


# ---------------------------------------------------------------------------
# THE ONLY PLACE a message type is defined. Add new types here.
# ---------------------------------------------------------------------------
MESSAGE_TYPES = {
    0x01: MsgSpec("DATA"),                  # any length <= MAX_PAYLOAD
    0x02: MsgSpec("ACK", exact_length=2),
    0x03: MsgSpec("PING", exact_length=0),
    0x04: MsgSpec("PONG", exact_length=0),
}


class _Context:
    """Per-frame scratch data (not parser state; State is the state)."""

    def __init__(self):
        self.msg_type = 0
        self.len_hi = 0
        self.length = 0
        self.remaining = 0
        self.payload = bytearray()
        self.crc = 0

    def reset(self):
        self.__init__()


class FsmParser:
    def __init__(self):
        self.state = State.SYNC1
        self.ctx = _Context()
        self.events = []
        # (state, event) -> count, for transition coverage reporting
        self.transition_hits = {}

    # -- byte -> event classification (depends on current state + context) --

    def _classify(self, b):
        s = self.state
        if s is State.SYNC1:
            return Event.MAGIC1_BYTE if b == MAGIC1 else Event.OTHER_BYTE
        if s is State.SYNC2:
            if b == MAGIC2:
                return Event.MAGIC2_BYTE
            if b == MAGIC1:
                return Event.MAGIC1_BYTE
            return Event.OTHER_BYTE
        if s is State.TYPE:
            return Event.KNOWN_TYPE if b in MESSAGE_TYPES else Event.UNKNOWN_TYPE
        if s is State.LEN_HI:
            return Event.ANY_BYTE
        if s is State.LEN_LO:
            length = (self.ctx.len_hi << 8) | b
            spec = MESSAGE_TYPES[self.ctx.msg_type]
            if spec.length_error(length) is not None:
                return Event.TYPE_LEN_BAD
            if length > MAX_PAYLOAD:
                return Event.LEN_TOO_BIG
            if length == 0:
                return Event.LEN_ZERO
            return Event.LEN_OK
        if s is State.PAYLOAD:
            return Event.PAYLOAD_LAST if self.ctx.remaining == 1 else Event.PAYLOAD_BYTE
        if s is State.CRC:
            return Event.CRC_OK if b == self.ctx.crc else Event.CRC_BAD
        raise UndefinedTransitionError("no classifier for state %s" % s)

    # -- actions (mutate context / emit output; never change state directly) --

    def _act_noop(self, b):
        pass

    def _act_start_frame(self, b):
        self.ctx.reset()

    def _act_err_bad_magic(self, b):
        self.events.append((
            "error",
            "bad magic: expected 0x%02X after 0x%02X, got 0x%02X"
            % (MAGIC2, MAGIC1, b),
        ))

    def _act_set_type(self, b):
        self.ctx.msg_type = b
        self.ctx.crc ^= b

    def _act_err_unknown_type(self, b):
        self.events.append(("error", "unknown message type 0x%02X" % b))
        self.ctx.reset()

    def _act_set_len_hi(self, b):
        self.ctx.len_hi = b
        self.ctx.crc ^= b

    def _act_set_len_lo(self, b):
        self.ctx.length = (self.ctx.len_hi << 8) | b
        self.ctx.remaining = self.ctx.length
        self.ctx.crc ^= b

    def _act_err_type_len(self, b):
        length = (self.ctx.len_hi << 8) | b
        spec = MESSAGE_TYPES[self.ctx.msg_type]
        self.events.append(("error", spec.length_error(length)))
        self.ctx.reset()

    def _act_err_len_too_big(self, b):
        length = (self.ctx.len_hi << 8) | b
        self.events.append((
            "error",
            "payload length %d exceeds maximum %d" % (length, MAX_PAYLOAD),
        ))
        self.ctx.reset()

    def _act_add_payload(self, b):
        self.ctx.payload.append(b)
        self.ctx.crc ^= b
        self.ctx.remaining -= 1

    def _act_emit_message(self, b):
        self.events.append((
            "message",
            {"type": MESSAGE_TYPES[self.ctx.msg_type].name,
             "payload": bytes(self.ctx.payload)},
        ))
        self.ctx.reset()

    def _act_err_crc(self, b):
        self.events.append((
            "error",
            "crc mismatch: computed 0x%02X, got 0x%02X" % (self.ctx.crc, b),
        ))
        self.ctx.reset()

    # -- the transition table (built once, see _build_transitions) --

    @staticmethod
    def _build_transitions():
        # actions are stored by name and resolved per-instance in _apply,
        # so the cached table never binds to the wrong parser object
        S, E = State, Event
        return {
            (S.SYNC1, E.MAGIC1_BYTE):  (S.SYNC2, "_act_noop"),
            (S.SYNC1, E.OTHER_BYTE):   (S.SYNC1, "_act_noop"),  # skip noise
            (S.SYNC2, E.MAGIC2_BYTE):  (S.TYPE, "_act_start_frame"),
            (S.SYNC2, E.MAGIC1_BYTE):  (S.SYNC2, "_act_noop"),  # 0xAB 0xAB
            (S.SYNC2, E.OTHER_BYTE):   (S.SYNC1, "_act_err_bad_magic"),
            (S.TYPE, E.KNOWN_TYPE):    (S.LEN_HI, "_act_set_type"),
            (S.TYPE, E.UNKNOWN_TYPE):  (S.SYNC1, "_act_err_unknown_type"),
            (S.LEN_HI, E.ANY_BYTE):    (S.LEN_LO, "_act_set_len_hi"),
            (S.LEN_LO, E.LEN_OK):      (S.PAYLOAD, "_act_set_len_lo"),
            (S.LEN_LO, E.LEN_ZERO):    (S.CRC, "_act_set_len_lo"),
            (S.LEN_LO, E.LEN_TOO_BIG): (S.SYNC1, "_act_err_len_too_big"),
            (S.LEN_LO, E.TYPE_LEN_BAD): (S.SYNC1, "_act_err_type_len"),
            (S.PAYLOAD, E.PAYLOAD_BYTE): (S.PAYLOAD, "_act_add_payload"),
            (S.PAYLOAD, E.PAYLOAD_LAST): (S.CRC, "_act_add_payload"),
            (S.CRC, E.CRC_OK):         (S.SYNC1, "_act_emit_message"),
            (S.CRC, E.CRC_BAD):        (S.SYNC1, "_act_err_crc"),
        }

    # -- public API (same shape as LegacyParser) --

    def feed(self, data):
        for b in data:
            self._step(b)
        return self.events

    def _step(self, b):
        self._apply(self.state, self._classify(b), b)

    def _apply(self, state, event, b):
        key = (state, event)
        transitions = self._transitions()
        if key not in transitions:
            raise UndefinedTransitionError(
                "undefined transition: %s x %s (byte 0x%02X)"
                % (state.name, event.name, b))
        self.transition_hits[key] = self.transition_hits.get(key, 0) + 1
        next_state, action_name = transitions[key]
        getattr(self, action_name)(b)
        self.state = next_state

    _TRANSITIONS_CACHE = None

    @classmethod
    def _transitions(cls):
        if cls._TRANSITIONS_CACHE is None:
            cls._TRANSITIONS_CACHE = cls._build_transitions()
        return cls._TRANSITIONS_CACHE


def defined_transitions():
    """Sorted list of all defined (state, event) pairs."""
    return sorted(FsmParser._transitions(),
                  key=lambda k: (k[0].name, k[1].name))


def coverage_report(hits):
    """Return (text, covered, total) transition coverage for a hits dict."""
    defined = defined_transitions()
    lines = []
    covered = 0
    for state, event in defined:
        n = hits.get((state, event), 0)
        if n:
            covered += 1
        lines.append("  %-8s x %-13s : %s" % (
            state.name, event.name, n if n else "MISS"))
    total = len(defined)
    header = "transition coverage: %d/%d defined transitions (%.1f%%)" % (
        covered, total, 100.0 * covered / total)
    undefined = (len(State) * len(Event)) - total
    footer = ("undefined (state, event) pairs: %d "
              "-> all raise UndefinedTransitionError" % undefined)
    return "\n".join([header] + lines + [footer]), covered, total


def transition_table_markdown():
    """Render the full state x event matrix as a Markdown table."""
    transitions = FsmParser._transitions()
    events = list(Event)
    lines = [
        "| State \\ Event | " + " | ".join(e.name for e in events) + " |",
        "|" + "---|" * (len(events) + 1),
    ]
    for state in State:
        cells = []
        for event in events:
            entry = transitions.get((state, event))
            if entry is None:
                cells.append("ERROR")
            else:
                next_state, action_name = entry
                cells.append("%s / %s" % (
                    next_state.name,
                    action_name.replace("_act_", "")))
        lines.append("| **%s** | %s |" % (state.name, " | ".join(cells)))
    return "\n".join(lines)


if __name__ == "__main__":
    print(transition_table_markdown())
