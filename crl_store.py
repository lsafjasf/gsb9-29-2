"""CRL (证书吊销列表) 快速查询库 —— 仅依赖 Python 3 标准库。

设计要点:
- 索引: 吊销序列号存入 frozenset, 查询为 O(1) 哈希查找, 与列表规模无关。
- 原子更新: 新列表在后台完整构建后, 通过一次引用赋值整体替换;
  读者不加锁, 永远看到完整的旧列表或完整的新列表, 不会看到半份。
- 新鲜度: 查询时强制校验签发方与有效期(this_update/next_update),
  过期或签发方不匹配时拒绝使用并抛出带原因的异常。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional, Union

Serial = Union[int, str]


# ---------------------------------------------------------------- 异常

class CRLError(Exception):
    """本库所有异常的基类。"""


class NoCRLLoadedError(CRLError):
    """尚未加载任何吊销列表。"""


class CRLFreshnessError(CRLError):
    """列表不可信(过期 / 未生效 / 签发方不匹配)的基类。"""


class CRLExpiredError(CRLFreshnessError):
    """next_update 已过: 列表过期, 可能缺少最近的吊销记录。"""


class CRLNotYetValidError(CRLFreshnessError):
    """this_update 在未来: 列表尚未生效(可能是时钟偏移或伪造列表)。"""


class IssuerMismatchError(CRLFreshnessError):
    """列表签发方与期望的证书签发 CA 不一致, 该列表无权为此证书背书。"""


class BadSerialError(CRLError, ValueError):
    """序列号无法解析。"""


# ---------------------------------------------------------------- 工具

def normalize_serial(serial: Serial) -> int:
    """把序列号统一为 int。

    接受 int 或十六进制字符串(可带 0x 前缀 / 冒号分隔 / 大小写不限)。
    """
    if isinstance(serial, bool):
        raise BadSerialError(f"非法序列号: {serial!r}")
    if isinstance(serial, int):
        if serial < 0:
            raise BadSerialError(f"序列号不能为负: {serial!r}")
        return serial
    if isinstance(serial, str):
        text = serial.strip().lower().replace(":", "")
        if text.startswith("0x"):
            text = text[2:]
        if not text:
            raise BadSerialError(f"非法序列号: {serial!r}")
        try:
            return int(text, 16)
        except ValueError:
            raise BadSerialError(f"无法解析的十六进制序列号: {serial!r}") from None
    raise BadSerialError(f"不支持的序列号类型: {type(serial).__name__}")


def _require_aware(moment: datetime, field: str) -> datetime:
    if moment.tzinfo is None:
        raise ValueError(f"{field} 必须带时区信息(建议 UTC)")
    return moment.astimezone(timezone.utc)


# ---------------------------------------------------------------- 列表

class RevocationList:
    """一份不可变的吊销列表。构建完成后内容不再变化, 可安全地被多线程共享。"""

    __slots__ = ("issuer", "this_update", "next_update", "_serials")

    def __init__(
        self,
        issuer: str,
        this_update: datetime,
        next_update: datetime,
        serials: Iterable[Serial],
    ) -> None:
        if not issuer:
            raise ValueError("issuer 不能为空")
        self.issuer = issuer
        self.this_update = _require_aware(this_update, "this_update")
        self.next_update = _require_aware(next_update, "next_update")
        if self.next_update <= self.this_update:
            raise ValueError("next_update 必须晚于 this_update")
        # frozenset: O(1) 查询 + 天然去重 + 不可变(线程安全共享)
        self._serials = frozenset(normalize_serial(s) for s in serials)

    def __len__(self) -> int:
        return len(self._serials)

    def __contains__(self, serial: Serial) -> bool:
        return normalize_serial(serial) in self._serials

    @property
    def serials(self) -> frozenset:
        return self._serials

    def check_fresh(self, issuer: Optional[str], at: datetime) -> None:
        """新鲜度 + 签发方校验。不可用时抛出 CRLFreshnessError 子类并说明原因。"""
        at = _require_aware(at, "at")
        if issuer is not None and issuer != self.issuer:
            raise IssuerMismatchError(
                f"吊销列表签发方为 {self.issuer!r}, 与期望的 {issuer!r} 不匹配; "
                "该列表无权裁定此 CA 签发的证书, 拒绝使用"
            )
        if at < self.this_update:
            raise CRLNotYetValidError(
                f"当前时间 {at.isoformat()} 早于列表生效时间 "
                f"{self.this_update.isoformat()}; 列表尚未生效, 拒绝使用"
            )
        if at >= self.next_update:
            raise CRLExpiredError(
                f"当前时间 {at.isoformat()} 已超过列表过期时间 "
                f"{self.next_update.isoformat()}; 列表可能缺少最新吊销记录, 拒绝使用"
            )


# ---------------------------------------------------------------- 存储

class CRLStore:
    """吊销列表的线程安全容器: 支持批量加载、增量更新与快速查询。"""

    def __init__(
        self,
        issuer: Optional[str] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._default_issuer = issuer
        self._clock = clock
        self._current: Optional[RevocationList] = None
        self._write_lock = threading.Lock()  # 只串行化写者; 读者无锁

    # ---------------- 查询(读路径, 无锁) ----------------

    def current(self) -> RevocationList:
        crl = self._current  # 单次引用读取, 要么旧要么新, 不会是一半
        if crl is None:
            raise NoCRLLoadedError("尚未加载任何吊销列表")
        return crl

    def is_revoked(self, serial: Serial, issuer: Optional[str] = None) -> bool:
        """查询序列号是否被吊销。

        返回 True/False; 列表不可用(未加载/过期/未生效/签发方不符)时抛异常。
        """
        crl = self.current()
        crl.check_fresh(issuer or self._default_issuer, self._clock())
        return normalize_serial(serial) in crl

    # ---------------- 批量加载(写路径, 原子替换) ----------------

    def load(
        self,
        serials: Iterable[Serial],
        this_update: datetime,
        next_update: datetime,
        issuer: Optional[str] = None,
    ) -> RevocationList:
        """批量构建并原子替换当前列表。

        构建期间抛异常(如数据源中断)时, 旧列表原样保留, 查询不受影响。
        """
        issuer = issuer or self._default_issuer
        if issuer is None:
            raise ValueError("必须通过 issuer 参数或构造函数指定签发方")
        # 先在锁外完整构建; 构建失败则旧列表不受影响
        new_crl = RevocationList(issuer, this_update, next_update, serials)
        with self._write_lock:
            self._current = new_crl  # 原子换入
        return new_crl

    def load_from_file(self, path: str) -> RevocationList:
        """从文本文件加载。格式:

            issuer: CN=Example CA
            this_update: 2026-09-30T00:00:00+00:00
            next_update: 2026-10-07T00:00:00+00:00
            0a1b2c
            ff:00:01
            # 以 # 开头的行为注释
        """
        header = {}
        serials = []
        with open(path, "r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, 1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line and not header_complete(header):
                    key, _, value = line.partition(":")
                    key = key.strip().lower()
                    if key in ("issuer", "this_update", "next_update"):
                        header[key] = value.strip()
                        continue
                try:
                    serials.append(normalize_serial(line))
                except BadSerialError as exc:
                    raise CRLError(f"{path}:{lineno}: {exc}") from exc
        missing = {"issuer", "this_update", "next_update"} - header.keys()
        if missing:
            raise CRLError(f"{path}: 缺少头部字段: {sorted(missing)}")
        return self.load(
            serials,
            this_update=datetime.fromisoformat(header["this_update"]),
            next_update=datetime.fromisoformat(header["next_update"]),
            issuer=header["issuer"],
        )

    # ---------------- 增量更新(读-改-写, 原子换入) ----------------

    def apply_delta(
        self,
        add: Iterable[Serial] = (),
        remove: Iterable[Serial] = (),
        this_update: Optional[datetime] = None,
        next_update: Optional[datetime] = None,
    ) -> RevocationList:
        """在当前列表基础上增删序列号, 原子发布新版本。

        增删集合先完整物化再构建, 中途异常不会污染当前列表。
        """
        additions = frozenset(normalize_serial(s) for s in add)
        removals = frozenset(normalize_serial(s) for s in remove)
        with self._write_lock:
            base = self.current()
            merged = (base.serials | additions) - removals
            new_crl = RevocationList(
                issuer=base.issuer,
                this_update=this_update or base.this_update,
                next_update=next_update or base.next_update,
                serials=merged,
            )
            self._current = new_crl
        return new_crl


def header_complete(header: dict) -> bool:
    return len(header) >= 3
