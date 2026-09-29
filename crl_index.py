"""CRL（证书吊销列表）索引库：快速查询、新鲜度校验、原子更新。仅使用标准库。

设计要点
--------
1. 索引结构：序列号存入 frozenset，查询为哈希查找，平均 O(1)，
   与列表规模无关（对比线性扫描 O(n)，见 bench.py 数据）。
2. 不可变快照：每次加载/更新先在本地完整构建一个 CRLSnapshot，
   构建成功后一次性替换引用。读线程拿到的永远是完整快照，
   不会看到"半份列表"；构建中途失败则旧快照原样保留。
3. 新鲜度：查询前校验 thisUpdate <= now <= nextUpdate，
   过期或未生效一律拒绝（抛 StaleCRLError / NotYetValidError）；
   加载与查询均校验签发方（issuer），不匹配抛 IssuerMismatchError。

列表文件格式（JSONL，便于流式解析超大列表）
------------------------------------------
首行为头部：{"issuer": "CN=CA", "thisUpdate": "...", "nextUpdate": "..."}
其后每行一个被吊销序列号（十进制整数或 0x 前缀十六进制）。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional, Union

Serial = Union[int, str]

_MAX = datetime.max.replace(tzinfo=timezone.utc)
_MIN = datetime.min.replace(tzinfo=timezone.utc)


class CRLError(Exception):
    """所有 CRL 相关错误的基类。"""


class StaleCRLError(CRLError):
    """列表已过期（now > nextUpdate），拒绝使用。"""


class NotYetValidError(CRLError):
    """列表尚未生效（now < thisUpdate），拒绝使用。"""


class IssuerMismatchError(CRLError):
    """列表签发方与期望的证书签发方不一致，拒绝使用。"""


class CRLFormatError(CRLError):
    """列表数据格式错误（含加载中断导致的截断）。"""


def normalize_serial(serial: Serial) -> int:
    """把序列号规范化为非负 int。接受 int、十进制字符串、0x 十六进制字符串。"""
    if isinstance(serial, bool):
        raise CRLFormatError("序列号不能是布尔值")
    if isinstance(serial, int):
        value = serial
    elif isinstance(serial, str):
        text = serial.strip()
        value = int(text, 16) if text.lower().startswith("0x") else int(text, 10)
    else:
        raise CRLFormatError(f"无法识别的序列号类型: {type(serial)!r}")
    if value < 0:
        raise CRLFormatError(f"序列号不能为负: {value}")
    return value


def parse_time(value: Union[str, datetime]) -> datetime:
    """解析 ISO 8601 时间，统一为带 UTC 时区的 datetime。"""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass(frozen=True)
class CRLSnapshot:
    """不可变快照。发布后绝不修改，是原子交换的基本单位。"""

    issuer: str
    this_update: datetime
    next_update: Optional[datetime]  # None 表示未声明过期时间
    serials: frozenset
    version: int

    @property
    def count(self) -> int:
        return len(self.serials)

    def check_fresh(self, now: datetime) -> None:
        if now < self.this_update:
            raise NotYetValidError(
                f"CRL 尚未生效: thisUpdate={self.this_update.isoformat()} > now={now.isoformat()}"
            )
        if self.next_update is not None and now > self.next_update:
            raise StaleCRLError(
                f"CRL 已过期: nextUpdate={self.next_update.isoformat()} < now={now.isoformat()}"
            )


class CRLIndex:
    """单个签发方（issuer）的吊销列表索引。线程安全：读无锁，写互斥。"""

    def __init__(self, issuer: str):
        self._issuer = issuer
        # 初始空快照的生效窗口为空区间（thisUpdate > nextUpdate），
        # 任何查询都会因新鲜度校验失败被拒绝：
        # "尚未加载列表"不等于"全部证书有效"。
        self._snapshot = CRLSnapshot(issuer, _MAX, _MIN, frozenset(), 0)
        self._write_lock = threading.Lock()

    # ---- 内部 ----

    def _check_issuer(self, issuer: str) -> None:
        if issuer != self._issuer:
            raise IssuerMismatchError(
                f"CRL 签发方不匹配: 期望 {self._issuer!r}, 实际 {issuer!r}"
            )

    def _publish(self, snapshot: CRLSnapshot) -> None:
        # 引用赋值在 CPython 中是原子的；加锁只为互斥多个写线程。
        with self._write_lock:
            self._snapshot = snapshot

    # ---- 加载与更新 ----

    def load(
        self,
        serials: Iterable[Serial],
        *,
        this_update: Union[str, datetime],
        next_update: Optional[Union[str, datetime]],
        issuer: Optional[str] = None,
    ) -> int:
        """批量全量加载。构建完成前任何异常都不会影响在用的旧快照。返回去重后的条目数。"""
        issuer = self._issuer if issuer is None else issuer
        self._check_issuer(issuer)
        # 在局部变量中完整构建；frozenset 自动去重。
        new_serials = frozenset(normalize_serial(s) for s in serials)
        snapshot = CRLSnapshot(
            issuer=issuer,
            this_update=parse_time(this_update),
            next_update=None if next_update is None else parse_time(next_update),
            serials=new_serials,
            version=self._snapshot.version + 1,
        )
        self._publish(snapshot)
        return snapshot.count

    def load_file(self, path: str) -> int:
        """从 JSONL 文件流式加载。文件截断/损坏会抛 CRLFormatError，旧快照保持不变。"""
        with open(path, "r", encoding="utf-8") as fh:
            return self.load_stream(line for line in fh if line.strip())

    def load_stream(self, lines: Iterator[str]) -> int:
        """从行迭代器加载（首行 JSON 头，其后每行一个序列号）。"""
        header_line = next(lines, None)
        if header_line is None:
            raise CRLFormatError("空输入：缺少头部行")
        try:
            header = json.loads(header_line)
            issuer = header["issuer"]
            this_update = header["thisUpdate"]
            next_update = header.get("nextUpdate")
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise CRLFormatError(f"头部解析失败: {exc}") from exc
        serials = []
        for lineno, line in enumerate(lines, start=2):
            try:
                serials.append(normalize_serial(line))
            except (ValueError, CRLFormatError) as exc:
                raise CRLFormatError(f"第 {lineno} 行序列号解析失败: {exc}") from exc
        return self.load(
            serials, this_update=this_update, next_update=next_update, issuer=issuer
        )

    def update(
        self,
        *,
        add: Iterable[Serial] = (),
        remove: Iterable[Serial] = (),
        this_update: Optional[Union[str, datetime]] = None,
        next_update: Optional[Union[str, datetime]] = None,
    ) -> int:
        """增量更新：在当前快照基础上增删条目，整体原子替换。返回更新后的条目数。"""
        base = self._snapshot
        new_serials = (base.serials | frozenset(normalize_serial(s) for s in add)) - frozenset(
            normalize_serial(s) for s in remove
        )
        snapshot = CRLSnapshot(
            issuer=base.issuer,
            this_update=base.this_update if this_update is None else parse_time(this_update),
            next_update=base.next_update if next_update is None else parse_time(next_update),
            serials=new_serials,
            version=base.version + 1,
        )
        self._publish(snapshot)
        return snapshot.count

    # ---- 查询 ----

    def is_revoked(
        self,
        serial: Serial,
        *,
        issuer: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        """查询序列号是否被吊销。

        新鲜度不满足（过期/未生效）或签发方不匹配时抛异常拒绝，
        绝不返回"未吊销"这种可能误导的结论。
        """
        snap = self._snapshot  # 单次引用读取，保证本次查询看到的是同一份完整快照
        if issuer is not None:
            self._check_issuer(issuer)
        snap.check_fresh(now or datetime.now(timezone.utc))
        return normalize_serial(serial) in snap.serials

    # ---- 状态 ----

    def status(self) -> dict:
        snap = self._snapshot
        return {
            "issuer": snap.issuer,
            "version": snap.version,
            "count": snap.count,
            "thisUpdate": snap.this_update.isoformat(),
            "nextUpdate": None if snap.next_update is None else snap.next_update.isoformat(),
        }
