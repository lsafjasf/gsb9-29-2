from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import scope
from .model import Cookie, RejectCode, SameSite, SendBlock, SendResult, SetResult
from .parsing import parse_set_cookie

DEFAULT_PUBLIC_SUFFIXES = frozenset({
    "com", "org", "net", "edu", "gov", "io", "dev", "app",
    "co.uk", "org.uk", "ac.uk", "co.jp", "com.cn", "com.au",
    "appspot.com", "github.io", "s3.amazonaws.com",
})


@dataclass(frozen=True)
class Policy:
    public_suffixes: frozenset = DEFAULT_PUBLIC_SUFFIXES
    max_cookie_bytes: int = 4096
    max_total_cookies: int = 3000
    max_per_domain: int = 180
    allow_secure_from_insecure_scheme: bool = False
    require_secure_for_samesite_none: bool = True
    strict_attributes: bool = False
    default_same_site_lax: bool = True
    secure_schemes: frozenset = frozenset({"https", "wss"})
    safe_methods: frozenset = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class CookieJar:
    def __init__(self, clock: Callable[[], float] = time.time, policy: Optional[Policy] = None):
        self._clock = clock
        self.policy = policy if policy is not None else Policy()
        self._cookies: Dict[Tuple[str, str, str], Cookie] = {}
        self._seq = 0

    def __len__(self) -> int:
        self._purge_expired()
        return len(self._cookies)

    def all_cookies(self) -> List[Cookie]:
        self._purge_expired()
        return list(self._cookies.values())

    def set_cookie(self, header: str, url: str, *, now: Optional[float] = None) -> SetResult:
        current = self._clock() if now is None else now
        parsed, error = parse_set_cookie(header)
        if error is not None:
            return SetResult(False, error)
        assert parsed is not None
        if self.policy.strict_attributes and parsed.attribute_error is not None:
            return SetResult(False, RejectCode.INVALID_ATTRIBUTE)
        if len((parsed.name + parsed.value).encode("utf-8")) > self.policy.max_cookie_bytes:
            return SetResult(False, RejectCode.COOKIE_TOO_LONG)
        parts = urlsplit(url)
        if not parts.hostname:
            return SetResult(False, RejectCode.INVALID_URL)
        host = scope.canonical_host(parts.hostname)
        scheme = parts.scheme.lower()

        domain, host_only = self._resolve_domain(parsed.domain, host)
        if domain is None:
            return SetResult(False, host_only)
        if parsed.secure and scheme not in self.policy.secure_schemes \
                and not self.policy.allow_secure_from_insecure_scheme:
            return SetResult(False, RejectCode.INSECURE_SCHEME)
        if parsed.same_site is SameSite.NONE and not parsed.secure \
                and self.policy.require_secure_for_samesite_none:
            return SetResult(False, RejectCode.SAMESITE_NONE_WITHOUT_SECURE)

        path = parsed.path if parsed.path else scope.default_path(parts.path)
        expires_at: Optional[float] = None
        if parsed.max_age is not None:
            expires_at = current + parsed.max_age
        elif parsed.expires_at is not None:
            expires_at = parsed.expires_at

        key = (parsed.name, domain, path)
        if expires_at is not None and expires_at <= current:
            removed = self._cookies.pop(key, None) is not None
            self._purge_expired()
            return SetResult(False, RejectCode.ALREADY_EXPIRED, removed_existing=removed)

        existing = self._cookies.get(key)
        if existing is not None:
            cookie = Cookie(
                name=parsed.name, value=parsed.value, domain=domain, path=path,
                host_only=host_only, secure=parsed.secure, http_only=parsed.http_only,
                same_site=parsed.same_site, expires_at=expires_at,
                creation_time=existing.creation_time, creation_seq=existing.creation_seq,
                last_access=current,
            )
            replaced = True
        else:
            self._seq += 1
            cookie = Cookie(
                name=parsed.name, value=parsed.value, domain=domain, path=path,
                host_only=host_only, secure=parsed.secure, http_only=parsed.http_only,
                same_site=parsed.same_site, expires_at=expires_at,
                creation_time=current, creation_seq=self._seq, last_access=current,
            )
            replaced = False
        self._cookies[key] = cookie
        evicted = self._enforce_limits(now=current)
        return SetResult(True, RejectCode.OK, cookie=cookie, evicted=evicted, replaced=replaced)

    def _resolve_domain(self, requested, host):
        if requested is None:
            return host, True
        if scope.is_public_suffix(requested, self.policy.public_suffixes):
            if requested == host:
                return host, True
            return None, RejectCode.PUBLIC_SUFFIX
        if scope.is_ip_literal(host):
            if requested != host:
                return None, RejectCode.IP_LITERAL_MISMATCH
            return host, False
        if host == requested or host.endswith("." + requested):
            return requested, False
        return None, RejectCode.INVALID_DOMAIN

    def cookies_for(self, url: str, *, same_site: str = "same-site",
                    method: str = "GET", top_level: bool = True,
                    for_http: bool = True, now: Optional[float] = None) -> SendResult:
        parts = urlsplit(url)
        host = scope.canonical_host(parts.hostname or "")
        scheme = parts.scheme.lower()
        request_path = parts.path or "/"
        current = self._clock() if now is None else now
        self._purge_expired(now=current)
        selected, excluded = [], []
        for cookie in self._cookies.values():
            block = self._send_block(
                cookie, host, scheme, request_path,
                same_site, method.upper(), top_level, for_http, current,
            )
            if block is None:
                cookie.last_access = current
                selected.append(cookie)
            else:
                excluded.append((cookie, block))
        selected.sort(key=lambda c: (-len(c.path), c.creation_seq))
        return SendResult(selected, excluded)

    def cookie_header(self, url: str, **kwargs) -> str:
        return self.cookies_for(url, **kwargs).header()

    def _send_block(self, cookie, host, scheme, request_path,
                    site_mode, method, top_level, for_http, current):
        if cookie.expires_at is not None and cookie.expires_at <= current:
            return SendBlock.EXPIRED
        if not scope.domain_match(host, cookie):
            return SendBlock.DOMAIN_MISMATCH
        if not scope.path_match(request_path, cookie.path):
            return SendBlock.PATH_MISMATCH
        if cookie.secure and scheme not in self.policy.secure_schemes:
            return SendBlock.NOT_SECURE_CONTEXT
        if cookie.http_only and not for_http:
            return SendBlock.HTTPONLY
        same_site_attr = cookie.same_site
        if same_site_attr is None and self.policy.default_same_site_lax:
            same_site_attr = SameSite.LAX
        if site_mode == "cross-site":
            if same_site_attr is SameSite.STRICT:
                return SendBlock.SAMESITE_STRICT
            if same_site_attr is SameSite.LAX and not (
                top_level and method in self.policy.safe_methods
            ):
                return SendBlock.SAMESITE_LAX
        return None

    def _purge_expired(self, now: Optional[float] = None) -> List[Cookie]:
        current = self._clock() if now is None else now
        expired = [c for c in self._cookies.values()
                   if c.expires_at is not None and c.expires_at <= current]
        for cookie in expired:
            del self._cookies[cookie.key()]
        return expired

    def _enforce_limits(self, now: Optional[float] = None) -> List[Cookie]:
        evicted = self._purge_expired(now=now)
        by_domain: Dict[str, List[Cookie]] = {}
        for cookie in self._cookies.values():
            by_domain.setdefault(cookie.domain, []).append(cookie)
        for cookies in by_domain.values():
            excess = len(cookies) - self.policy.max_per_domain
            if excess > 0:
                victims = sorted(cookies, key=self._evict_key)[:excess]
                for victim in victims:
                    del self._cookies[victim.key()]
                    evicted.append(victim)
        while len(self._cookies) > self.policy.max_total_cookies:
            victim = min(self._cookies.values(), key=self._evict_key)
            del self._cookies[victim.key()]
            evicted.append(victim)
        return evicted

    @staticmethod
    def _evict_key(cookie: Cookie):
        return (cookie.last_access, cookie.creation_seq)
