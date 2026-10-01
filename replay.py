"""防重放存储：内存有上界 + JSON 持久化，重启后仍能拒绝旧口令。

上界机制（两层）
----------------
1. 每用户只保留“仍可能被接受”的窗口计数器：
   校验器只会接受 [cur+est-back, cur+est+fwd] 内的计数器，
   因此早于 cur - retention_windows 的记录永久失效，可安全淘汰；
   每用户条目数另有硬上限 max_counters_per_user（超出时淘汰最旧）。
2. 全局限额 max_users：LRU 淘汰最久未活动的用户状态。
   注意：若某用户状态被 LRU 整体淘汰，其在保留期内的旧口令
   理论上可再次通过——这是有界内存的固有折衷，部署时应把
   max_users 设为大于活跃用户数的值。

持久化
------
每次 record 后以“写临时文件 + 原子 rename”的方式落盘 JSON；
重启后用同一路径构造即可恢复，旧口令仍被拒绝。
"""
from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from typing import Callable, Hashable, Optional, Set


class ReplayStore:
    def __init__(
        self,
        path: Optional[str] = None,
        max_users: int = 4096,
        max_counters_per_user: int = 32,
        retention_windows: int = 16,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_users < 1 or max_counters_per_user < 1 or retention_windows < 0:
            raise ValueError("限额参数非法")
        self.path = path
        self.max_users = max_users
        self.max_counters_per_user = max_counters_per_user
        self.retention_windows = retention_windows
        self.clock = clock
        # user -> set[counter]，OrderedDict 实现 LRU
        self._used: "OrderedDict[Hashable, Set[int]]" = OrderedDict()
        if path is not None:
            self._load()

    # ---------- 查询 ----------

    def seen(self, user: Hashable, counter: int) -> bool:
        counters = self._used.get(user)
        if counters is None:
            return False
        self._used.move_to_end(user)
        return counter in counters

    def user_count(self) -> int:
        return len(self._used)

    def counter_count(self, user: Hashable) -> int:
        counters = self._used.get(user)
        return len(counters) if counters else 0

    # ---------- 写入 ----------

    def record(self, user: Hashable, counter: int, current_counter: int) -> None:
        """记录已用口令，并顺带淘汰过期/超量条目，随后持久化。"""
        counters = self._used.setdefault(user, set())
        self._used.move_to_end(user)
        counters.add(int(counter))
        self._prune_user(counters, current_counter)
        while len(self._used) > self.max_users:
            self._used.popitem(last=False)  # 淘汰最久未活动的用户
        self._save()

    def prune(self, current_counter: int) -> None:
        """主动淘汰所有用户的过期计数器。"""
        for counters in self._used.values():
            self._prune_user(counters, current_counter)
        self._save()

    def _prune_user(self, counters: Set[int], current_counter: int) -> None:
        floor = current_counter - self.retention_windows
        expired = [c for c in counters if c < floor]
        for c in expired:
            counters.discard(c)
        # 硬上限：超出时淘汰最旧（最小）的计数器
        while len(counters) > self.max_counters_per_user:
            counters.discard(min(counters))

    # ---------- 持久化 ----------

    def _save(self) -> None:
        if self.path is None:
            return
        data = {str(u): sorted(cs) for u, cs in self._used.items()}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)  # 原子替换，避免半截文件

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)  # 文件损坏时直接抛错（fail-closed，宁可拒绝服务）
        if not isinstance(data, dict):
            raise ValueError("持久化文件格式非法")
        for user, counters in data.items():
            self._used[user] = {int(c) for c in counters}
        while len(self._used) > self.max_users:
            self._used.popitem(last=False)
