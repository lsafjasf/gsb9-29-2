"""Append-only rotation timeline."""
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class TimelineEvent:
    at: float
    event: str
    details: Dict[str, Any] = field(default_factory=dict)


_SCALAR = (str, int, float, bool)


class Timeline:
    def __init__(self) -> None:
        self.events: List[TimelineEvent] = []

    def record(self, at: float, event: str, **details: Any) -> None:
        self.events.append(TimelineEvent(at=at, event=event, details=details))

    def names(self) -> List[str]:
        return [e.event for e in self.events]

    def render(self) -> str:
        if not self.events:
            return "(empty timeline)"
        base = self.events[0].at
        lines = []
        for e in self.events:
            bits = []
            for k, v in e.details.items():
                if k == "rotation_id" or not isinstance(v, _SCALAR):
                    continue
                bits.append(f"{k}={v}")
            suffix = ("  " + " ".join(bits)).rstrip()
            lines.append(f"t={e.at - base:+9.1f}s  {e.event:<22}{suffix}")
        return "\n".join(lines)
