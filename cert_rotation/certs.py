"""Certificate value object."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Certificate:
    cert_id: str
    not_before: float
    not_after: float
    payload: str = ""

    def is_valid_at(self, t: float) -> bool:
        return self.not_before <= t < self.not_after
