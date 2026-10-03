"""断点状态与幂等副作用存储：JSON 原子落盘，支持进程崩溃恢复。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any, Callable, Dict, List, Optional

from .model import RUNNING, PENDING


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class CheckpointStore:
    """保存：各步骤状态/尝试次数/耗时、成功输出、事件时间线、效果账本、回滚记录。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.data: Dict[str, Any] = {
            "version": 1,
            "status": "RUNNING",
            "steps": {},          # id -> {status, attempts, duration, error, skipped_reason}
            "outputs": {},        # id -> 成功步骤返回值（可 JSON 序列化）
            "events": [],         # 时间线事件，含单调序号 seq
            "ledger": [],         # 幂等副作用账本（效果日志）
            "rollback": [],       # 回滚记录
            "seq": 0,
        }

    # ---- 生命周期 ----
    def load_or_init(self, step_ids: List[str]) -> None:
        if os.path.exists(self.path):
            self.data = _read_json(self.path)
        else:
            for sid in step_ids:
                self.data["steps"][sid] = {
                    "status": PENDING, "attempts": 0, "duration": 0.0,
                    "error": "", "skipped_reason": "", "rollback_status": "",
                }
            self._save()

    def recover_stale_running(self) -> List[str]:
        """进程崩溃时停留在 RUNNING 的步骤视为中断，重置为 PENDING 重跑。

        其已产生的副作用由幂等账本保证不重复。
        """
        recovered = []
        for sid, info in self.data["steps"].items():
            if info["status"] == RUNNING:
                # 崩溃中的尝试不计入重试预算，重新派发
                info["status"] = PENDING
                info["attempts"] = 0
                recovered.append(sid)
        if recovered:
            self._save()
        return recovered

    # ---- 步骤状态 ----
    def step_status(self, sid: str) -> str:
        return self.data["steps"][sid]["status"]

    def step_info(self, sid: str) -> Dict[str, Any]:
        return self.data["steps"][sid]

    def update_step(self, sid: str, **fields: Any) -> None:
        with self._lock:
            self.data["steps"][sid].update(fields)
            self._save()

    def set_run_status(self, status: str) -> None:
        with self._lock:
            self.data["status"] = status
            self._save()

    def record_output(self, sid: str, value: Any) -> None:
        with self._lock:
            self.data["outputs"][sid] = value
            self._save()

    def record_rollback(self, record: Dict[str, Any]) -> None:
        with self._lock:
            self.data["rollback"].append(record)
            self._save()

    # ---- 事件与账本 ----
    def log_event(self, kind: str, **fields: Any) -> None:
        with self._lock:
            self.data["seq"] += 1
            event = {"seq": self.data["seq"], "kind": kind}
            event.update(fields)
            self.data["events"].append(event)
            self._save()

    def append_ledger(self, entry: Dict[str, Any]) -> None:
        with self._lock:
            self.data["seq"] += 1
            entry = dict(entry)
            entry["seq"] = self.data["seq"]
            self.data["ledger"].append(entry)
            self._save()

    def _save(self) -> None:
        _atomic_write_json(self.path, self.data)


class IdempotencyStore:
    """幂等副作用存储：key = (step_id, key)。

    get_or_run 先查账本：已记录则直接返回缓存结果，不重复执行 fn。
    崩溃恢复后账本仍在磁盘上，因此重跑步骤不会产生重复副作用。
    """

    def __init__(self, checkpoint: CheckpointStore) -> None:
        self._checkpoint = checkpoint
        self._lock = threading.Lock()

    def _find(self, step_id: str, key: str) -> Optional[Dict[str, Any]]:
        for entry in self._checkpoint.data["ledger"]:
            if entry.get("step") == step_id and entry.get("key") == key:
                return entry
        return None

    def get_or_run(self, step_id: str, key: str, fn: Callable[[], Any]) -> Any:
        with self._lock:
            hit = self._find(step_id, key)
            if hit is not None:
                return hit.get("result")
            result = fn()
            self._checkpoint.append_ledger(
                {"step": step_id, "key": key, "result": result})
            return result

    def ledger(self) -> List[Dict[str, Any]]:
        return list(self._checkpoint.data["ledger"])
