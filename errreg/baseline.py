"""JSON storage for error baselines, one file per problem case."""

from __future__ import annotations

import json
from pathlib import Path

from .core import Baseline


def baseline_path(directory, name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return Path(directory) / f"{safe}.json"


def save_baseline(directory, baseline: Baseline) -> Path:
    path = baseline_path(directory, baseline.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline.to_dict(), indent=2) + "\n",
                    encoding="utf-8")
    return path


def load_baseline(directory, name: str) -> Baseline:
    path = baseline_path(directory, name)
    data = json.loads(path.read_text(encoding="utf-8"))
    return Baseline.from_dict(data)


def has_baseline(directory, name: str) -> bool:
    return baseline_path(directory, name).exists()
