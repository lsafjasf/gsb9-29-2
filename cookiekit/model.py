from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional, Tuple, List


class SameSite(enum.Enum):
    STRICT = "Strict"
    LAX = "Lax"
    NONE = "None"

    @classmethod
    def parse(cls, raw: str) -> Optional["SameSite"]:
        if raw is None:
            return None
        lowered = raw.strip().lower()
        for member in cls:
            if member.value.lower() == lowered:
                return member
        return None


class RejectCode(enum.Enum):
    OK = "OK"
    EMPTY_HEADER = "EMPTY_HEADER"
    MISSING_EQUALS = "MISSING_EQUALS"
    EMPTY_NAME = "EMPTY_NAME"
    INVALID_URL = "INVALID_URL"
    COOKIE_TOO_LONG = "COOKIE_TOO_LONG"
    INVALID_DOMAIN = "INVALID_DOMAIN"
    PUBLIC_SUFFIX = "PUBLIC_SUFFIX"
    IP_LITERAL_MISMATCH = "IP_LITERAL_MISMATCH"
    ALREADY_EXPIRED = "ALREADY_EXPIRED"
    INSECURE_SCHEME = "INSECURE_SCHEME"
    SAMESITE_NONE_WITHOUT_SECURE = "SAMESITE_NONE_WITHOUT_SECURE"
    INVALID_ATTRIBUTE = "INVALID_ATTRIBUTE"


class SendBlock(enum.Enum):
    EXPIRED = "EXPIRED"
    DOMAIN_MISMATCH = "DOMAIN_MISMATCH"
    PATH_MISMATCH = "PATH_MISMATCH"
    NOT_SECURE_CONTEXT = "NOT_SECURE_CONTEXT"
    SAMESITE_STRICT = "SAMESITE_STRICT"
    SAMESITE_LAX = "SAMESITE_LAX"
    HTTPONLY = "HTTPONLY"


@dataclass
class Cookie:
    name: str
    value: str
    domain: str
    path: str
    host_only: bool = True
    secure: bool = False
    http_only: bool = False
    same_site: Optional[SameSite] = None
    expires_at: Optional[float] = None
    creation_time: float = 0.0
    last_access: float = 0.0
    creation_seq: int = 0

    @property
    def persistent(self) -> bool:
        return self.expires_at is not None

    def key(self) -> Tuple[str, str, str]:
        return (self.name, self.domain, self.path)

    def header_value(self) -> str:
        return f"{self.name}={self.value}"


@dataclass
class SetResult:
    accepted: bool
    code: RejectCode
    cookie: Optional[Cookie] = None
    evicted: List[Cookie] = None
    replaced: bool = False
    removed_existing: bool = False

    def __post_init__(self):
        if self.evicted is None:
            self.evicted = []


@dataclass
class SendResult:
    cookies: List[Cookie]
    excluded: List[Tuple[Cookie, SendBlock]]

    def header(self) -> str:
        return "; ".join(c.header_value() for c in self.cookies)

    def names(self) -> List[str]:
        return [c.name for c in self.cookies]
