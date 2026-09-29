"""Lazy resolution of nested secret references in configuration files.

Config files contain only *references*, never plaintext secrets:

    {"db": {"password": "${secret:db/password}"}}

Reference syntax: ``${secret:NAME}`` inside any string value.

* ``NAME`` may itself contain references (nested names), e.g.
  ``${secret:${secret:which-db}}``.
* A fetched secret value may contain further references (nested values),
  resolved recursively.
* Secrets are fetched strictly on demand: nothing is fetched until a
  value that needs it is actually requested.
* Resolved values never appear in logs or exception messages; only
  reference names and config paths are reported.

Standard library only (Python 3.8+).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

__all__ = [
    "SecretRefError",
    "ParseError",
    "EmptyReferenceError",
    "CircularReferenceError",
    "ResolutionError",
    "SecretNotFoundError",
    "SecretServiceError",
    "SecretTimeoutError",
    "Text",
    "Ref",
    "parse_template",
    "Resolver",
    "LazyConfig",
    "LazyMapping",
    "LazySequence",
]

_TOKEN = "${secret:"
_LOG = logging.getLogger("secret_refs")


# ------------------------------------------------------------------ errors

class SecretRefError(Exception):
    """Base class for all errors raised by this module.

    Contract: messages contain reference names and config paths only --
    never resolved secret values, never raw provider error messages.
    """


class ParseError(SecretRefError):
    """Malformed reference syntax."""

    def __init__(self, message: str, path: str) -> None:
        self.path = path
        super().__init__(f"{message} (at {path})")


class EmptyReferenceError(SecretRefError):
    """A ${secret:...} reference whose name resolves to an empty string."""


class CircularReferenceError(SecretRefError):
    """A reference cycle; ``chain`` is the full resolution path."""

    def __init__(self, chain: Tuple[str, ...]) -> None:
        self.chain = tuple(chain)
        super().__init__(
            "circular reference detected: " + " -> ".join(self.chain)
        )


class ResolutionError(SecretRefError):
    """A reference could not be resolved."""


class SecretNotFoundError(ResolutionError):
    """The secret service reported the name as unknown (KeyError)."""


class SecretServiceError(ResolutionError):
    """The secret service was unavailable or returned an error."""


class SecretTimeoutError(SecretServiceError):
    """The secret service did not answer within the configured timeout."""


# --------------------------------------------------------------------- AST

@dataclass(frozen=True)
class Text:
    """Literal text node."""

    value: str


@dataclass(frozen=True)
class Ref:
    """Reference node; ``name_parts`` is a tuple of Text/Ref nodes."""

    name_parts: Tuple[Any, ...]


def parse_template(text: str, path: str = "<template>") -> List[Any]:
    """Parse *text* into a list of Text/Ref nodes. Never touches the network."""
    if not isinstance(text, str):
        raise TypeError("parse_template expects a str")
    nodes, _ = _parse(text, 0, path, expect_close=False)
    return nodes


def _parse(text: str, pos: int, path: str, expect_close: bool):
    nodes: List[Any] = []
    buf: List[str] = []
    i = pos
    n = len(text)
    while i < n:
        if text.startswith(_TOKEN, i):
            if buf:
                nodes.append(Text("".join(buf)))
                buf = []
            inner, i = _parse(text, i + len(_TOKEN), path, expect_close=True)
            nodes.append(Ref(tuple(inner)))
        elif text[i] == "}" and expect_close:
            if buf:
                nodes.append(Text("".join(buf)))
            return nodes, i + 1
        else:
            buf.append(text[i])
            i += 1
    if expect_close:
        raise ParseError("unterminated reference, missing '}'", path)
    if buf:
        nodes.append(Text("".join(buf)))
    return nodes, i


# ---------------------------------------------------------------- resolver

class Resolver:
    """Resolves references by fetching secrets from a provider on demand.

    ``fetch`` is a callable ``(name: str) -> str``. It is invoked at most
    once per secret name (results are cached) and only when a requested
    value actually depends on that secret.
    """

    def __init__(
        self,
        fetch: Callable[[str], str],
        timeout: float = 5.0,
        logger: Optional[logging.Logger] = None,
        cache: bool = True,
    ) -> None:
        if not callable(fetch):
            raise TypeError("fetch must be callable")
        self._fetch = fetch
        self._timeout = float(timeout)
        self._log = logger if logger is not None else _LOG
        self._cache: Optional[dict] = {} if cache else None
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="secret-refs"
        )

    def close(self) -> None:
        self._executor.shutdown(wait=True)

    def __enter__(self) -> "Resolver":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False

    def resolve_text(self, text: str, path: str = "<template>") -> str:
        """Resolve all references in *text*. *path* identifies the location
        for error reporting (e.g. ``config.json:db.password``)."""
        nodes = parse_template(text, path)
        return self._resolve_nodes(nodes, (path,))

    def _resolve_nodes(self, nodes: List[Any], chain: Tuple[str, ...]) -> str:
        parts: List[str] = []
        for node in nodes:
            if isinstance(node, Text):
                parts.append(node.value)
            elif isinstance(node, Ref):
                parts.append(self._resolve_ref(node, chain))
            else:  # pragma: no cover - defensive
                raise TypeError(f"unknown node type: {type(node).__name__}")
        return "".join(parts)

    def _resolve_ref(self, ref: Ref, chain: Tuple[str, ...]) -> str:
        name = self._resolve_nodes(list(ref.name_parts), chain).strip()
        where = " -> ".join(chain)
        if not name:
            raise EmptyReferenceError(f"empty secret reference (at {where})")
        label = f"secret:{name}"
        if label in chain:
            raise CircularReferenceError(chain + (label,))
        value = self._fetch_secret(name, chain)
        # A secret value may itself contain references: resolve recursively.
        sub_nodes = parse_template(value, label)
        return self._resolve_nodes(sub_nodes, chain + (label,))

    def _fetch_secret(self, name: str, chain: Tuple[str, ...]) -> str:
        if self._cache is not None and name in self._cache:
            return self._cache[name]
        where = " -> ".join(chain)
        self._log.info("fetching secret %s (at %s)", name, where)
        future = self._executor.submit(self._fetch, name)
        try:
            value = future.result(timeout=self._timeout)
        except _FutureTimeout:
            future.cancel()
            self._log.warning("timeout fetching secret %s (at %s)", name, where)
            raise SecretTimeoutError(
                f"timeout after {self._timeout:g}s fetching secret "
                f"'{name}' (at {where})"
            ) from None
        except KeyError:
            raise SecretNotFoundError(
                f"secret '{name}' not found (at {where})"
            ) from None
        except SecretRefError:
            raise
        except Exception as exc:
            # NOTE: str(exc) is deliberately dropped -- provider error
            # messages may contain sensitive material. Only the error
            # *type* is reported, never its message.
            self._log.warning(
                "secret service error %s while fetching %s",
                type(exc).__name__,
                name,
            )
            raise SecretServiceError(
                f"secret service unavailable ({type(exc).__name__}) "
                f"while fetching '{name}' (at {where})"
            ) from None
        if not isinstance(value, str):
            # Do not echo the offending value back.
            raise ResolutionError(
                f"secret service returned a non-string value for "
                f"'{name}' (at {where})"
            )
        self._log.info("fetched secret %s", name)
        if self._cache is not None:
            self._cache[name] = value
        return value


# ------------------------------------------------------------- lazy config

def _resolve_value(node: Any, resolver: Resolver, path: str) -> Any:
    if isinstance(node, str):
        return resolver.resolve_text(node, path)
    if isinstance(node, dict):
        return LazyMapping(node, resolver, path)
    if isinstance(node, list):
        return LazySequence(node, resolver, path)
    return node


class LazyMapping(Mapping):
    """Dict view that resolves each value only when it is accessed."""

    __slots__ = ("_data", "_resolver", "_path")

    def __init__(self, data: dict, resolver: Resolver, path: str) -> None:
        self._data = data
        self._resolver = resolver
        self._path = path

    def __getitem__(self, key: str) -> Any:
        node = self._data[key]
        return _resolve_value(node, self._resolver, f"{self._path}.{key}")

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        # Never resolve values for display.
        return f"<LazyMapping at {self._path} ({len(self._data)} keys)>"


class LazySequence(Sequence):
    """List view that resolves each item only when it is accessed."""

    __slots__ = ("_data", "_resolver", "_path")

    def __init__(self, data: list, resolver: Resolver, path: str) -> None:
        self._data = data
        self._resolver = resolver
        self._path = path

    def __getitem__(self, index):
        node = self._data[index]
        return _resolve_value(node, self._resolver, f"{self._path}[{index}]")

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"<LazySequence at {self._path} ({len(self._data)} items)>"


class LazyConfig:
    """A parsed config document whose references resolve on demand."""

    def __init__(self, data: Any, resolver: Resolver, source: str = "<config>") -> None:
        self._data = data
        self._resolver = resolver
        self._source = source

    @classmethod
    def from_json(
        cls, text: str, resolver: Resolver, source: str = "<json>"
    ) -> "LazyConfig":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ParseError(f"invalid JSON: {exc.msg}", f"{source}:{exc.lineno}") from None
        return cls(data, resolver, source)

    @classmethod
    def from_json_file(cls, filename: str, resolver: Resolver) -> "LazyConfig":
        with open(filename, "r", encoding="utf-8") as fh:
            return cls.from_json(fh.read(), resolver, source=filename)

    def get(self, dotted_path: str = "") -> Any:
        """Resolve one config value on demand.

        ``dotted_path`` uses dots for dict keys and numbers for list
        indices, e.g. ``"db.password"`` or ``"servers.0"``. Only the
        secrets needed by the requested value are fetched.
        """
        parts = [p for p in dotted_path.split(".") if p] if dotted_path else []
        node = self._data
        for part in parts:
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                raise ResolutionError(
                    f"config path not found: {dotted_path!r} (at {self._source})"
                )
        path = self._source + ("." + dotted_path if dotted_path else "")
        return _resolve_value(node, self._resolver, path)
