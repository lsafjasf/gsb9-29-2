"""secretref — 配置密钥引用解析与按需注入（仅 Python 标准库）。

引用语法
--------
配置文件中只允许写引用，不允许写明文密钥::

    ${secret:<密钥路径>}

例如 ``password = "${secret:prod/db/password}"``。

嵌套
----
密钥服务返回的内容本身也可以是引用，解析器会递归展开::

    prod/db/password  ->  "${secret:shared/rotate-key}"
    shared/rotate-key ->  "actual-secret"   # 最终值

安全约束
--------
* 按需注入：只有被访问到的配置项才会向密钥服务发请求（见 LazyConfig）。
* 异常信息与日志中只出现引用路径（config 键名 / secret 路径），
  绝不出现解析出来的密钥值；密钥服务抛出的异常链也会被抑制，
  防止其消息里夹带的内容经 traceback 泄漏。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterator, Mapping, Optional, Protocol, runtime_checkable

# 匹配 ${secret:KEY}；KEY 内不允许再出现花括号（嵌套靠密钥值递归实现）。
_REF_RE = re.compile(r"\$\{secret:([^{}]*)\}")
# 检测写坏了的引用，如 "${secret:abc"（未闭合）。
_BROKEN_REF_RE = re.compile(r"\$\{(?::|secret:)[^}]*$")


@runtime_checkable
class SecretService(Protocol):
    """密钥服务接口：按路径取密钥。"""

    def get(self, key: str, *, timeout: Optional[float] = None) -> str:
        ...


class SecretRefError(Exception):
    """所有引用解析错误的基类。

    消息中只允许出现路径信息，子类必须保证不拼接任何密钥值。
    """


class RefParseError(SecretRefError):
    """引用语法错误（空引用、引用未闭合等）。"""


class EmptyRefError(RefParseError):
    def __init__(self, chain: list[str]) -> None:
        super().__init__(f"empty secret reference at {format_chain(chain)}")
        self.chain = list(chain)


class UnterminatedRefError(RefParseError):
    def __init__(self, chain: list[str]) -> None:
        super().__init__(f"unterminated secret reference at {format_chain(chain)}")
        self.chain = list(chain)


class FetchError(SecretRefError):
    """向密钥服务取值失败（不可用 / 超时 / 返回非法类型）。"""

    def __init__(self, key: str, chain: list[str], detail: str) -> None:
        super().__init__(f"{detail} for secret {key!r} at {format_chain(chain)}")
        self.key = key
        self.chain = list(chain)


class SecretUnavailableError(FetchError):
    def __init__(self, key: str, chain: list[str], error_type: str) -> None:
        super().__init__(key, chain, f"secret service unavailable ({error_type})")


class SecretTimeoutError(FetchError):
    def __init__(self, key: str, chain: list[str]) -> None:
        super().__init__(key, chain, "secret service timed out")


class DepthExceededError(SecretRefError):
    def __init__(self, chain: list[str], max_depth: int) -> None:
        super().__init__(
            f"max nesting depth {max_depth} exceeded at {format_chain(chain)}"
        )
        self.chain = list(chain)
        self.max_depth = max_depth


class CycleError(SecretRefError):
    """循环引用：消息给出完整路径环，如 a -> b -> a。"""

    def __init__(self, chain: list[str]) -> None:
        super().__init__("cyclic secret reference: " + " -> ".join(chain))
        self.chain = list(chain)


def format_chain(chain: list[str]) -> str:
    return " -> ".join(chain) if chain else "<unknown>"


class Resolver:
    """把含引用的字符串解析为最终值。

    每次 ``resolve`` 独立维护解析栈，同一个 Resolver 可安全复用；
    解析结果不写日志、不放入异常消息。
    """

    def __init__(
        self,
        service: SecretService,
        *,
        timeout: Optional[float] = None,
        max_depth: int = 16,
    ) -> None:
        if max_depth < 1:
            raise ValueError("max_depth must be >= 1")
        self._service = service
        self._timeout = timeout
        self._max_depth = max_depth

    def resolve(self, text: str, *, origin: str = "config:<root>") -> str:
        """解析单个字符串。origin 用于在报错中指出配置路径。"""
        if not isinstance(text, str):
            raise TypeError("resolve() expects a str")
        return self._expand(text, [origin], depth=0)

    def _expand(self, text: str, chain: list[str], depth: int) -> str:
        if depth > self._max_depth:
            raise DepthExceededError(chain, self._max_depth)
        if _BROKEN_REF_RE.search(text):
            raise UnterminatedRefError(chain)

        def replace(match: "re.Match[str]") -> str:
            key = match.group(1).strip()
            entry = f"secret:{key}"
            if not key:
                raise EmptyRefError(chain)
            if entry in chain:
                raise CycleError(chain + [entry])
            chain.append(entry)
            try:
                raw = self._fetch(key, chain)
                return self._expand(raw, chain, depth + 1)
            finally:
                chain.pop()

        return _REF_RE.sub(replace, text)

    def _fetch(self, key: str, chain: list[str]) -> str:
        try:
            raw = self._service.get(key, timeout=self._timeout)
        except SecretRefError:
            raise
        except TimeoutError:
            # from None：抑制异常链，防止服务异常消息里夹带的内容经 traceback 泄漏。
            raise SecretTimeoutError(key, chain) from None
        except Exception as exc:  # 密钥服务任何故障统一包装
            raise SecretUnavailableError(key, chain, type(exc).__name__) from None
        if not isinstance(raw, str):
            raise SecretUnavailableError(key, chain, "non-str result")
        return raw


_MISSING = object()


class LazyConfig:
    """按需注入的配置视图。

    * 构造时不触发任何密钥请求；
    * 只有 ``get`` / ``__getitem__`` 实际访问到的键才解析；
    * 解析结果缓存，同一键只取一次密钥。
    """

    def __init__(self, data: Mapping[str, Any], resolver: Resolver) -> None:
        self._data: Dict[str, Any] = dict(data)
        self._resolver = resolver
        self._cache: Dict[str, Any] = {}

    def get(self, key: str, default: Any = _MISSING) -> Any:
        if key in self._cache:
            return self._cache[key]
        if key not in self._data:
            if default is _MISSING:
                raise KeyError(key)
            return default
        value = self._data[key]
        if isinstance(value, str):
            value = self._resolver.resolve(value, origin=f"config:{key}")
        self._cache[key] = value
        return value

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def keys(self) -> Iterator[str]:
        return iter(self._data)
