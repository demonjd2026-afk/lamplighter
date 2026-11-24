"""Telemetry access.

`TelemetrySource` is the only interface the rest of the system uses. The
JSON-lines adapter below reads the demo's files; a production deployment
implements the same four methods over its own metrics, logs and change feed.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Protocol


class TelemetrySource(Protocol):
    def requests(self, service: str, start: float, end: float) -> list[dict]: ...
    def calls(self, service: str, start: float, end: float) -> list[dict]: ...
    def changes(self, start: float, end: float) -> list[dict]: ...
    def catalog(self) -> dict: ...


class _Tail:
    """Incremental reader of one append-only JSON-lines file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.partial = b""
        self.skipped = 0

    def read_new(self) -> list[dict]:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return []
        if size < self.offset:  # truncated or rotated
            self.offset, self.partial = 0, b""
        if size == self.offset:
            return []
        with open(self.path, "rb") as fh:
            fh.seek(self.offset)
            chunk = fh.read(size - self.offset)
        self.offset = size
        data = self.partial + chunk
        lines = data.split(b"\n")
        self.partial = lines.pop()  # an incomplete trailing line waits for the next read
        records = []
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                self.skipped += 1
                continue
            if isinstance(record, dict) and isinstance(record.get("ts"), (int, float)):
                records.append(record)
            else:
                self.skipped += 1
        return records


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _well_formed(record: dict) -> bool:
    """Telemetry is input from outside: accept only records with the expected field types."""
    if not isinstance(record.get("service"), str) or not _number(record.get("latency_ms")):
        return False
    if not isinstance(record.get("status"), int) or isinstance(record.get("status"), bool):
        return False
    error = record.get("error")
    if record.get("kind") == "request":
        return error is None or isinstance(error, dict)
    if record.get("kind") == "call":
        return isinstance(record.get("target"), str) and (error is None or isinstance(error, str))
    return False


class JsonlTelemetry:
    """Reads <root>/logs/<service>.jsonl, <root>/changes.jsonl and <root>/catalog.json."""

    SLACK_S = 10.0  # records may be written slightly out of order

    def __init__(self, root: Path, retention_s: float = 7200.0) -> None:
        self._root = Path(root)
        self._retention = retention_s
        self._lock = threading.Lock()
        self._tails: dict[str, _Tail] = {}
        self._requests: dict[str, list[dict]] = {}
        self._calls: dict[str, list[dict]] = {}
        self._changes: list[dict] = []
        self._change_tail = _Tail(self._root / "changes.jsonl")

    def _refresh(self) -> None:
        logs = self._root / "logs"
        if logs.is_dir():
            for path in logs.glob("*.jsonl"):
                tail = self._tails.setdefault(path.name, _Tail(path))
                for record in tail.read_new():
                    if not _well_formed(record):
                        tail.skipped += 1
                        continue
                    bucket = self._requests if record["kind"] == "request" else self._calls
                    bucket.setdefault(record["service"], []).append(record)
        for change in self._change_tail.read_new():
            if (change.get("kind") in ("deploy", "config", "restart") and isinstance(change.get("service"), str)
                    and isinstance(change.get("id"), str)):
                self._changes.append(change)
            else:
                self._change_tail.skipped += 1
        self._trim()

    def _trim(self) -> None:
        for bucket in (self._requests, self._calls):
            for service, records in bucket.items():
                if records and records[-1]["ts"] - records[0]["ts"] > self._retention * 1.5:
                    cutoff = records[-1]["ts"] - self._retention
                    bucket[service] = [r for r in records if r["ts"] >= cutoff]

    def _slice(self, records: list[dict], start: float, end: float) -> list[dict]:
        out = []
        for record in reversed(records):
            ts = record["ts"]
            if ts < start - self.SLACK_S:
                break
            if start <= ts < end:
                out.append(record)
        out.reverse()
        return out

    def requests(self, service: str, start: float, end: float) -> list[dict]:
        with self._lock:
            self._refresh()
            return self._slice(self._requests.get(service, []), start, end)

    def calls(self, service: str, start: float, end: float) -> list[dict]:
        with self._lock:
            self._refresh()
            return self._slice(self._calls.get(service, []), start, end)

    def changes(self, start: float, end: float) -> list[dict]:
        with self._lock:
            self._refresh()
            return [c for c in self._changes if start <= c["ts"] < end]

    def catalog(self) -> dict:
        try:
            catalog = json.loads((self._root / "catalog.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"services": {}}
        return catalog if isinstance(catalog.get("services"), dict) else {"services": {}}

    def skipped_lines(self) -> int:
        with self._lock:
            return sum(t.skipped for t in self._tails.values()) + self._change_tail.skipped
