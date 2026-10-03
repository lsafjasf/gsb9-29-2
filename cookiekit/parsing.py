from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from typing import Optional, Tuple

from .model import RejectCode, SameSite

_DATE_DELIMS = re.compile(r"[\x09\x20-\x2f\x3b-\x40\x5b-\x60\x7b-\x7e]+")
_TIME_RE = re.compile(r"(\d{1,2}):(\d{1,2}):(\d{1,2})(?:\D.*)?$")
_DAY_RE = re.compile(r"(\d{1,2})(?:\D.*)?$")
_MONTH_RE = re.compile(r"([A-Za-z]{3}).*$")
_YEAR_RE = re.compile(r"(\d{2,4})(?:\D.*)?$")
_MAX_AGE_RE = re.compile(r"-?\d+$")
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}


def parse_cookie_date(value: str) -> Optional[float]:
    hour = minute = second = day = month = year = None
    for token in _DATE_DELIMS.split(value):
        if not token:
            continue
        if hour is None:
            m = _TIME_RE.fullmatch(token)
            if m:
                hour, minute, second = (int(g) for g in m.groups())
                continue
        if day is None:
            m = _DAY_RE.fullmatch(token)
            if m:
                day = int(m.group(1))
                continue
        if month is None:
            m = _MONTH_RE.fullmatch(token)
            if m and m.group(1).lower() in _MONTHS:
                month = _MONTHS[m.group(1).lower()]
                continue
        if year is None:
            m = _YEAR_RE.fullmatch(token)
            if m:
                year = int(m.group(1))
                continue
    if hour is None or day is None or month is None or year is None:
        return None
    if 70 <= year <= 99:
        year += 1900
    elif 0 <= year <= 69:
        year += 2000
    if year < 1601 or not 1 <= day <= 31:
        return None
    if hour > 23 or minute > 59 or second > 59:
        return None
    try:
        return float(calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0)))
    except (ValueError, OverflowError):
        return None


@dataclass
class ParsedSetCookie:
    name: str
    value: str
    expires_at: Optional[float] = None
    max_age: Optional[int] = None
    domain: Optional[str] = None
    path: Optional[str] = None
    secure: bool = False
    http_only: bool = False
    same_site: Optional[SameSite] = None
    attribute_error: Optional[str] = None


def parse_set_cookie(header: Optional[str]) -> Tuple[Optional[ParsedSetCookie], Optional[RejectCode]]:
    if header is None or not header.strip():
        return None, RejectCode.EMPTY_HEADER
    parts = header.split(";")
    pair = parts[0]
    if "=" not in pair:
        return None, RejectCode.MISSING_EQUALS
    name, _, value = pair.partition("=")
    name = name.strip()
    value = value.strip()
    if not name:
        return None, RejectCode.EMPTY_NAME
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    parsed = ParsedSetCookie(name=name, value=value)
    for chunk in parts[1:]:
        chunk = chunk.strip()
        if not chunk:
            continue
        attr, _, attr_val = chunk.partition("=")
        attr_name = attr.strip().lower()
        attr_val = attr_val.strip()
        if attr_name == "expires":
            ts = parse_cookie_date(attr_val)
            if ts is not None:
                parsed.expires_at = ts
        elif attr_name == "max-age":
            if _MAX_AGE_RE.fullmatch(attr_val):
                parsed.max_age = int(attr_val)
        elif attr_name == "domain":
            domain = attr_val.lower().strip(".")
            if domain:
                parsed.domain = domain
        elif attr_name == "path":
            if attr_val.startswith("/"):
                parsed.path = attr_val
        elif attr_name == "secure":
            parsed.secure = True
        elif attr_name == "httponly":
            parsed.http_only = True
        elif attr_name == "samesite":
            same_site = SameSite.parse(attr_val)
            if same_site is None:
                parsed.attribute_error = f"SameSite={attr_val!r}"
            else:
                parsed.same_site = same_site
    return parsed, None
