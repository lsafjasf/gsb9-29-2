"""Legacy streaming frame parser (pre-refactor baseline).

Behavior is FROZEN: this module is the reference oracle for the differential
tests. It is intentionally written in the original style -- a pile of
boolean flags, implicit stages and early returns. Do not "clean up";
any behavioral change here breaks the differential tests.

Frame layout (all multi-byte integers big-endian):
    0xAB 0xCD | TYPE(1) | LEN(2) | PAYLOAD(LEN) | CRC(1)
CRC = XOR of all bytes from TYPE through the end of PAYLOAD.

Type-specific length rules (scattered inline below, see _feed_byte):
    0x01 DATA  any length <= 1024
    0x02 ACK   length must be exactly 2
    0x03 PING  length must be exactly 0
    0x04 PONG  length must be exactly 0
"""

MAGIC1 = 0xAB
MAGIC2 = 0xCD
MAX_PAYLOAD = 1024

VALID_TYPES = (0x01, 0x02, 0x03, 0x04)
TYPE_NAMES = {0x01: "DATA", 0x02: "ACK", 0x03: "PING", 0x04: "PONG"}


class LegacyParser:
    def __init__(self):
        # sync hunting
        self.half = False          # saw MAGIC1, waiting for MAGIC2
        self.locked = False        # full magic seen, inside a frame
        # header stages
        self.got_type = False
        self.got_len_hi = False
        self.got_len = False
        # body stages
        self.in_payload = False
        self.expect_crc = False
        # per-frame context
        self.msg_type = 0
        self.len_hi = 0
        self.length = 0
        self.remaining = 0
        self.payload = bytearray()
        self.crc = 0
        # output
        self.events = []

    def feed(self, data):
        for b in data:
            self._feed_byte(b)
        return self.events

    def _unlock(self):
        self.locked = False
        self.half = False
        self.got_type = False
        self.got_len_hi = False
        self.got_len = False
        self.in_payload = False
        self.expect_crc = False
        self.msg_type = 0
        self.len_hi = 0
        self.length = 0
        self.remaining = 0
        self.payload = bytearray()
        self.crc = 0

    def _feed_byte(self, b):
        if not self.locked:
            # hunting for the magic prefix
            if self.half:
                self.half = False
                if b == MAGIC2:
                    self.locked = True
                    return
                if b == MAGIC1:
                    self.half = True  # 0xAB 0xAB: second one may start magic
                    return
                self.events.append((
                    "error",
                    "bad magic: expected 0x%02X after 0x%02X, got 0x%02X"
                    % (MAGIC2, MAGIC1, b),
                ))
                return
            if b == MAGIC1:
                self.half = True
            return

        if not self.got_type:
            if b not in VALID_TYPES:
                self.events.append((
                    "error",
                    "unknown message type 0x%02X" % b,
                ))
                self._unlock()
                return
            self.got_type = True
            self.msg_type = b
            self.crc ^= b
            return

        if not self.got_len_hi:
            self.got_len_hi = True
            self.len_hi = b
            self.crc ^= b
            return

        if not self.got_len:
            self.got_len = True
            self.length = (self.len_hi << 8) | b
            self.crc ^= b
            # type-specific length rules, checked before the global max
            if self.msg_type in (0x03, 0x04) and self.length != 0:
                self.events.append((
                    "error",
                    "message %s must have length 0, got %d"
                    % (TYPE_NAMES[self.msg_type], self.length),
                ))
                self._unlock()
                return
            if self.msg_type == 0x02 and self.length != 2:
                self.events.append((
                    "error",
                    "message ACK must have length 2, got %d" % self.length,
                ))
                self._unlock()
                return
            if self.length > MAX_PAYLOAD:
                self.events.append((
                    "error",
                    "payload length %d exceeds maximum %d"
                    % (self.length, MAX_PAYLOAD),
                ))
                self._unlock()
                return
            if self.length == 0:
                self.expect_crc = True
            else:
                self.in_payload = True
                self.remaining = self.length
            return

        if self.in_payload:
            self.payload.append(b)
            self.crc ^= b
            self.remaining -= 1
            if self.remaining == 0:
                self.in_payload = False
                self.expect_crc = True
            return

        if self.expect_crc:
            if b != self.crc:
                self.events.append((
                    "error",
                    "crc mismatch: computed 0x%02X, got 0x%02X"
                    % (self.crc, b),
                ))
                self._unlock()
                return
            self.events.append((
                "message",
                {"type": TYPE_NAMES[self.msg_type],
                 "payload": bytes(self.payload)},
            ))
            self._unlock()
            return
