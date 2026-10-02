"""X.509 时间类型（UTCTime / GeneralizedTime）解析。"""

import re
from datetime import datetime, timezone

from .der import TAG_GENERALIZEDTIME, TAG_UTCTIME
from .errors import DerError

_UTC_RE = re.compile(rb"^(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})Z$")
_GEN_RE = re.compile(rb"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})Z$")


def parse_time(tlv) -> datetime:
    """把 UTCTime / GeneralizedTime 解析为带 UTC 时区的 datetime。"""
    raw = tlv.content
    if tlv.tag == TAG_UTCTIME:
        match = _UTC_RE.match(raw)
        if not match:
            raise DerError("UTCTime 格式非法: %r" % raw, code="cert.bad_time")
        year = int(match.group(1))
        # RFC 5280: YY < 50 => 20YY, 否则 19YY
        year += 2000 if year < 50 else 1900
        groups = match.groups()
    elif tlv.tag == TAG_GENERALIZEDTIME:
        match = _GEN_RE.match(raw)
        if not match:
            raise DerError("GeneralizedTime 格式非法: %r" % raw,
                           code="cert.bad_time")
        year = int(match.group(1))
        groups = match.groups()
    else:
        raise DerError("期望时间类型，实际标签 %d" % tlv.tag,
                       code="der.unexpected_tag")
    try:
        return datetime(
            year,
            int(groups[1]), int(groups[2]),
            int(groups[3]), int(groups[4]), int(groups[5]),
            tzinfo=timezone.utc,
        )
    except ValueError as exc:
        raise DerError("时间字段取值非法: %s" % exc, code="cert.bad_time")
