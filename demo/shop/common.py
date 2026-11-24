"""Shared helpers for the demo shop services (standard library only)."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


class JsonlLog:
    """Append-only JSON-lines log, one record per line, safe across threads."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self._fh = open(path, "a", encoding="utf-8", buffering=1)

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            self._fh.write(line + "\n")

    def close(self) -> None:
        with self._lock:
            self._fh.close()


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON so that readers never observe a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class JsonFile:
    """Reads a small JSON file, re-reading only when it changes on disk."""

    def __init__(self, path: Path, default: Any) -> None:
        self._path = path
        self._default = default
        self._sig: tuple[int, int] | None = None
        self._value: Any = default
        self._lock = threading.Lock()

    def get(self) -> Any:
        try:
            st = self._path.stat()
        except FileNotFoundError:
            return self._default
        sig = (st.st_mtime_ns, st.st_size)
        with self._lock:
            if sig != self._sig:
                try:
                    self._value = json.loads(self._path.read_text(encoding="utf-8"))
                    self._sig = sig
                except (json.JSONDecodeError, OSError):
                    pass  # keep the last good value
            return self._value


def now() -> float:
    return time.time()
