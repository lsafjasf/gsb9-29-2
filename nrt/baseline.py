"""Baseline storage: per-case error / iteration / timing distributions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Union

SCHEMA_VERSION = 1


@dataclass
class CaseBaseline:
    name: str
    kind: str  # "deterministic" | "randomized"
    errors: List[float]
    iterations: List[float]
    times_ms: List[float]
    tolerances: Dict[str, float] = field(default_factory=dict)
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "errors": self.errors,
            "iterations": self.iterations,
            "times_ms": self.times_ms,
            "tolerances": self.tolerances,
            "note": self.note,
        }

    @staticmethod
    def from_dict(data: dict) -> "CaseBaseline":
        return CaseBaseline(
            name=data["name"],
            kind=data["kind"],
            errors=list(data["errors"]),
            iterations=list(data["iterations"]),
            times_ms=list(data["times_ms"]),
            tolerances=dict(data.get("tolerances", {})),
            note=data.get("note", ""),
        )


@dataclass
class BaselineStore:
    cases: Dict[str, CaseBaseline]
    created_utc: str
    version: int = SCHEMA_VERSION

    def save(self, path: Union[str, Path]) -> None:
        payload = {
            "version": self.version,
            "created_utc": self.created_utc,
            "cases": {name: case.as_dict() for name, case in self.cases.items()},
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    @staticmethod
    def load(path: Union[str, Path]) -> "BaselineStore":
        payload = json.loads(Path(path).read_text())
        if payload.get("version") != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported baseline schema: {payload.get('version')!r}, "
                f"expected {SCHEMA_VERSION}"
            )
        cases = {
            name: CaseBaseline.from_dict(data)
            for name, data in payload["cases"].items()
        }
        return BaselineStore(cases=cases, created_utc=payload["created_utc"],
                             version=payload["version"])


def utc_now_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
