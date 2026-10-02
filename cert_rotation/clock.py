"""Injectable clock."""
import time


class Clock:
    def now(self) -> float:
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> float:
        return time.time()


class FakeClock(Clock):
    """Deterministic clock for tests / demos: only moves when advanced."""

    def __init__(self, start: float = 1_700_000_000.0):
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move clock backwards")
        self._t += float(seconds)
