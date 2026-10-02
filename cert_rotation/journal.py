"""WAL journal + atomic checkpoint.

Every state transition is appended as one JSON line.  A crash can at worst
leave a single torn final line; read_all() tolerates that and reports it.
The checkpoint file is written via tmp-file + os.replace (atomic on POSIX
and Windows) so recovery always has a last-known-good weight set even if
the whole journal is lost/corrupted.
"""
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple


class Journal:
    def __init__(self, path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, entry: Dict[str, Any]) -> None:
        line = json.dumps(entry, sort_keys=True, separators=(",", ":"))
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    def read_all(self) -> Tuple[List[Dict[str, Any]], bool]:
        """Return (entries, truncated). Stops at the first corrupt line."""
        entries: List[Dict[str, Any]] = []
        if not self.path.exists():
            return entries, False
        truncated = False
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    truncated = True
                    break
        return entries, truncated


def write_checkpoint(path, data: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_checkpoint(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
