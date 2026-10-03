from __future__ import annotations

import ipaddress
from typing import FrozenSet


def canonical_host(host: str) -> str:
    host = (host or "").strip().lower()
    if host.endswith("."):
        host = host[:-1]
    return host


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def domain_match(request_host: str, cookie) -> bool:
    if cookie.host_only:
        return request_host == cookie.domain
    if request_host == cookie.domain:
        return True
    if is_ip_literal(request_host):
        return False
    return request_host.endswith("." + cookie.domain)


def default_path(uri_path: str) -> str:
    if not uri_path or not uri_path.startswith("/"):
        return "/"
    if uri_path.count("/") == 1:
        return "/"
    return uri_path[: uri_path.rindex("/")]


def path_match(request_path: str, cookie_path: str) -> bool:
    if not request_path:
        request_path = "/"
    if request_path == cookie_path:
        return True
    if request_path.startswith(cookie_path):
        if cookie_path.endswith("/"):
            return True
        return request_path[len(cookie_path)] == "/"
    return False


def is_public_suffix(domain: str, public_suffixes: FrozenSet[str]) -> bool:
    if domain in public_suffixes:
        return True
    labels = domain.split(".")
    if len(labels) >= 2 and "*." + ".".join(labels[1:]) in public_suffixes:
        return True
    return False


def registrable_domain(host: str, public_suffixes: FrozenSet[str]) -> str:
    if is_ip_literal(host):
        return host
    labels = host.split(".")
    for i in range(len(labels)):
        candidate = ".".join(labels[i:])
        if is_public_suffix(candidate, public_suffixes):
            return ".".join(labels[i - 1:]) if i > 0 else host
    return host


def same_site(host_a: str, host_b: str, public_suffixes: FrozenSet[str]) -> bool:
    a = registrable_domain(canonical_host(host_a), public_suffixes)
    b = registrable_domain(canonical_host(host_b), public_suffixes)
    return a == b
