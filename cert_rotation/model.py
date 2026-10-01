"""证书与网关模拟模型（标准库实现）。

核心建模约定（贴近 TLS 真实语义）：
- 握手时必须选择一张**当前有效**（not_before <= now < not_after）的证书，
  否则握手失败；流量比例通过 100 个哈希槽分配。
- 一旦连接建立，其使用的证书指纹会被连接自身持有；之后证书下线、
  过期或回滚都不影响已建立的连接（TLS 会话不会重新握手校验）。
- 时间全部来自可注入的 Clock，测试可完全确定性地推进时间。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


class Clock:
    """单调可控时钟，单位为秒（epoch 秒）。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("time cannot go backwards")
        self._now += seconds
        return self._now


@dataclass(frozen=True)
class Certificate:
    """证书的最小抽象：指纹、有效期。"""

    serial: str
    not_before: float
    not_after: float

    def valid_at(self, now: float) -> bool:
        return self.not_before <= now < self.not_after

    def expired_at(self, now: float) -> bool:
        return now >= self.not_after

    def remaining(self, now: float) -> float:
        """剩余有效期（秒），已过期返回负数。"""
        return self.not_after - now


class HandshakeError(Exception):
    """没有可用证书或选到的证书无效时抛出。"""


@dataclass
class Connection:
    """已建立的连接：记住握手时使用的证书，后续不再受配置变化影响。"""

    conn_id: int
    client_id: str
    cert_serial: str

    def serve(self) -> str:
        """已建立连接永远可继续服务，不依赖证书是否仍被安装。"""
        return f"conn#{self.conn_id} via cert {self.cert_serial}"


@dataclass
class Gateway:
    """模拟 TLS 网关：证书安装集合 + 流量比例（旧, 新）。

    installed 为当前物理安装的证书；pct_old 为走旧证书的流量百分比。
    所有变更方法都返回快照，便于时间线与不变量检查。
    """

    clock: Clock
    installed: Dict[str, Certificate] = field(default_factory=dict)
    pct_old: int = 100
    old_serial: Optional[str] = None
    new_serial: Optional[str] = None
    _next_conn: int = 0

    # ---- 配置变更（每个方法都是原子操作，崩溃可发生在调用边界）----

    def install(self, cert: Certificate) -> None:
        if cert.serial in self.installed:
            raise RuntimeError(f"certificate {cert.serial} already installed")
        self.installed[cert.serial] = cert

    def set_split(self, pct_old: int, old_serial: str, new_serial: str) -> None:
        if not 0 <= pct_old <= 100:
            raise ValueError("pct_old must be in [0,100]")
        if pct_old > 0 and old_serial not in self.installed:
            raise RuntimeError("old certificate missing while traffic still needs it")
        if pct_old < 100 and new_serial not in self.installed:
            raise RuntimeError("new certificate missing while traffic needs it")
        self.pct_old = pct_old
        self.old_serial = old_serial
        self.new_serial = new_serial

    def remove(self, serial: str) -> None:
        if serial == self.old_serial and self.pct_old > 0:
            raise RuntimeError("refuse to remove certificate still carrying traffic")
        if serial == self.new_serial and self.pct_old < 100:
            raise RuntimeError("refuse to remove certificate still carrying traffic")
        self.installed.pop(serial, None)
        if self.old_serial == serial:
            self.old_serial = None
        if self.new_serial == serial:
            self.new_serial = None

    # ---- 流量面 ----

    def _pick(self, client_id: str) -> Certificate:
        if not (0 <= self.pct_old <= 100):
            raise HandshakeError("invalid split configuration")
        bucket = hash(client_id) % 100
        if bucket < self.pct_old:
            serial = self.old_serial
        else:
            serial = self.new_serial
        cert = self.installed.get(serial) if serial else None
        if cert is None:
            raise HandshakeError(f"no certificate installed for client {client_id}")
        now = self.clock.now()
        if not cert.valid_at(now):
            raise HandshakeError(
                f"certificate {cert.serial} selected but not valid at t={now:g}"
            )
        return cert

    def handshake(self, client_id: str) -> Connection:
        """新客户端握手；失败抛 HandshakeError。"""
        cert = self._pick(client_id)
        conn = Connection(self._next_conn, client_id, cert.serial)
        self._next_conn += 1
        return conn

    def handshakes(self, client_ids: List[str]) -> Tuple[List[Connection], List[str]]:
        """批量握手，返回 (成功连接, 失败客户端)。"""

        ok: List[Connection] = []
        failed: List[str] = []
        for cid in client_ids:
            try:
                ok.append(self.handshake(cid))
            except HandshakeError:
                failed.append(cid)
        return ok, failed

    def serving_certificates(self) -> Tuple[bool, bool]:
        """返回 (旧证书是否承载流量, 新证书是否承载流量)。"""
        old_up = self.pct_old > 0 and self.old_serial in self.installed
        new_up = self.pct_old < 100 and self.new_serial in self.installed
        return old_up, new_up

    def snapshot(self) -> dict:
        return {
            "installed": sorted(self.installed),
            "pct_old": self.pct_old,
            "old": self.old_serial,
            "new": self.new_serial,
        }
