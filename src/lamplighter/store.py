"""Durable state in SQLite: incidents, actions (idempotent), approvals and a
hash-chained audit log. One writer process; every mutation is a transaction."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator
from contextlib import contextmanager

OPEN_STATES = ("DETECTED", "PLANNED", "AWAITING_APPROVAL", "PREFLIGHT", "GATE", "ACTING", "VERIFYING", "REVERTING")
TERMINAL_STATES = ("RESOLVED", "SELF_RESOLVED", "ESCALATED")
GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
  id TEXT PRIMARY KEY, service TEXT NOT NULL, state TEXT NOT NULL,
  opened_at REAL NOT NULL, updated_at REAL NOT NULL, closed_at REAL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS incidents_state ON incidents(state);
CREATE TABLE IF NOT EXISTS actions (
  key TEXT PRIMARY KEY, incident_id TEXT NOT NULL, kind TEXT NOT NULL, params TEXT NOT NULL,
  status TEXT NOT NULL, result TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS approvals (
  incident_id TEXT PRIMARY KEY, decision TEXT NOT NULL, actor TEXT NOT NULL, plan_hash TEXT NOT NULL,
  decided_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, incident_id TEXT, event TEXT NOT NULL,
  payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    # ---- audit -----------------------------------------------------------
    def _audit(self, db: sqlite3.Connection, incident_id: str | None, event: str, payload: Any, ts: float) -> str:
        row = db.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        prev = row["hash"] if row else GENESIS
        body = canonical(payload)
        entry_hash = hashlib.sha256(canonical([prev, ts, incident_id, event, body]).encode()).hexdigest()
        db.execute("INSERT INTO audit(ts, incident_id, event, payload, prev_hash, hash) VALUES (?,?,?,?,?,?)",
                   (ts, incident_id, event, body, prev, entry_hash))
        return entry_hash

    def audit(self, incident_id: str | None, event: str, payload: Any, ts: float | None = None) -> str:
        with self._tx() as db:
            return self._audit(db, incident_id, event, payload, time.time() if ts is None else ts)

    def audit_trail(self, incident_id: str | None = None) -> list[dict]:
        with self._lock:
            if incident_id is None:
                rows = self._db.execute("SELECT * FROM audit ORDER BY seq").fetchall()
            else:
                rows = self._db.execute("SELECT * FROM audit WHERE incident_id=? ORDER BY seq", (incident_id,)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def verify_audit(self) -> tuple[bool, int, int | None]:
        """Recompute the chain. Returns (intact, entries, first_bad_seq)."""
        prev, count = GENESIS, 0
        with self._lock:
            rows = self._db.execute("SELECT * FROM audit ORDER BY seq").fetchall()
        for row in rows:
            expected = hashlib.sha256(
                canonical([prev, row["ts"], row["incident_id"], row["event"], row["payload"]]).encode()).hexdigest()
            if row["prev_hash"] != prev or row["hash"] != expected:
                return False, count, row["seq"]
            prev, count = row["hash"], count + 1
        return True, count, None

    # ---- incidents -------------------------------------------------------
    @staticmethod
    def _incident(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "service": row["service"], "state": row["state"], "opened_at": row["opened_at"],
                "updated_at": row["updated_at"], "closed_at": row["closed_at"], "data": json.loads(row["data"])}

    def open_incident(self, service: str, alert: dict, now: float) -> dict | None:
        """Open an incident unless one is already open for the service (deduplication)."""
        marks = ",".join("?" * len(OPEN_STATES))
        with self._tx() as db:
            if db.execute(f"SELECT 1 FROM incidents WHERE service=? AND state IN ({marks})",
                          (service, *OPEN_STATES)).fetchone():
                return None
            stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
            incident_id = f"inc-{stamp}-{secrets.token_hex(3)}"
            data = {"alert": alert, "attempts": {}, "timeline": []}
            db.execute("INSERT INTO incidents VALUES (?,?,?,?,?,?,?)",
                       (incident_id, service, "DETECTED", now, now, None, canonical(data)))
            self._audit(db, incident_id, "incident_opened", {"service": service, "alert": alert}, now)
            return {"id": incident_id, "service": service, "state": "DETECTED", "opened_at": now,
                    "updated_at": now, "closed_at": None, "data": data}

    def get_incident(self, incident_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        return self._incident(row) if row else None

    def list_incidents(self, limit: int = 50, open_only: bool = False) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            if open_only:
                marks = ",".join("?" * len(OPEN_STATES))
                rows = self._db.execute(
                    f"SELECT * FROM incidents WHERE state IN ({marks}) ORDER BY opened_at", OPEN_STATES).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM incidents ORDER BY opened_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._incident(r) for r in rows]

    def transition(self, incident: dict, new_state: str, event: str, detail: Any, now: float) -> dict:
        """Persist the incident and its state change, and audit it, atomically."""
        if new_state not in OPEN_STATES and new_state not in TERMINAL_STATES:
            raise ValueError(f"unknown state {new_state}")
        incident["data"]["timeline"].append({"ts": now, "from": incident["state"], "to": new_state, "event": event})
        closed = now if new_state in TERMINAL_STATES else None
        with self._tx() as db:
            current = db.execute("SELECT state FROM incidents WHERE id=?", (incident["id"],)).fetchone()
            if current is None or current["state"] in TERMINAL_STATES:
                raise RuntimeError("incident is closed or missing")
            db.execute("UPDATE incidents SET state=?, updated_at=?, closed_at=?, data=? WHERE id=?",
                       (new_state, now, closed, canonical(incident["data"]), incident["id"]))
            self._audit(db, incident["id"], event, {"from": incident["state"], "to": new_state, "detail": detail}, now)
        incident["state"], incident["updated_at"], incident["closed_at"] = new_state, now, closed
        return incident

    def save_data(self, incident: dict, now: float) -> None:
        with self._tx() as db:
            db.execute("UPDATE incidents SET updated_at=?, data=? WHERE id=?",
                       (now, canonical(incident["data"]), incident["id"]))

    # ---- actions (exactly-once intent, at-least-once delivery) ------------
    def action_begin(self, key: str, incident_id: str, kind: str, params: dict, now: float) -> dict:
        with self._tx() as db:
            row = db.execute("SELECT * FROM actions WHERE key=?", (key,)).fetchone()
            if row is None:
                db.execute("INSERT INTO actions VALUES (?,?,?,?,?,?,?,?)",
                           (key, incident_id, kind, canonical(params), "PENDING", None, now, now))
                self._audit(db, incident_id, "action_intent", {"key": key, "kind": kind, "params": params}, now)
                return {"key": key, "status": "PENDING", "result": None}
            return {"key": key, "status": row["status"], "result": json.loads(row["result"]) if row["result"] else None}

    def action_finish(self, key: str, status: str, result: Any, now: float) -> None:
        with self._tx() as db:
            row = db.execute("SELECT incident_id, kind FROM actions WHERE key=?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            db.execute("UPDATE actions SET status=?, result=?, updated_at=? WHERE key=?",
                       (status, canonical(result), now, key))
            self._audit(db, row["incident_id"], "action_result",
                        {"key": key, "kind": row["kind"], "status": status, "result": result}, now)

    def actions_since(self, since: float) -> int:
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) AS n FROM actions WHERE created_at>=? AND status!='PENDING'",
                                   (since,)).fetchone()
        return int(row["n"])

    def actions_for(self, incident_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM actions WHERE incident_id=? ORDER BY created_at",
                                    (incident_id,)).fetchall()
        return [{**dict(r), "params": json.loads(r["params"]), "result": json.loads(r["result"]) if r["result"] else None}
                for r in rows]

    # ---- approvals -------------------------------------------------------
    def decide(self, incident_id: str, decision: str, actor: str, plan_hash: str, now: float) -> bool:
        """Record a human decision for the plan currently awaiting approval. First decision wins."""
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be approved or rejected")
        with self._tx() as db:
            row = db.execute("SELECT state, data FROM incidents WHERE id=?", (incident_id,)).fetchone()
            if row is None or row["state"] != "AWAITING_APPROVAL":
                return False
            if json.loads(row["data"]).get("plan_hash") != plan_hash:
                return False
            if db.execute("SELECT 1 FROM approvals WHERE incident_id=?", (incident_id,)).fetchone():
                return False
            db.execute("INSERT INTO approvals VALUES (?,?,?,?,?)", (incident_id, decision, actor[:64], plan_hash, now))
            self._audit(db, incident_id, f"human_{decision}", {"actor": actor[:64], "plan_hash": plan_hash}, now)
            return True

    def approval(self, incident_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM approvals WHERE incident_id=?", (incident_id,)).fetchone()
        return dict(row) if row else None

    # ---- key/value -------------------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        with self._tx() as db:
            db.execute("INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, canonical(value)))

    def lease(self, owner: str, ttl_s: float, now: float) -> bool:
        """Single-worker guard: take or renew the lease unless another live owner holds it."""
        with self._tx() as db:
            row = db.execute("SELECT value FROM kv WHERE key='lease'").fetchone()
            held = json.loads(row["value"]) if row else None
            if held and held["owner"] != owner and held["expires"] > now:
                return False
            db.execute("INSERT INTO kv(key, value) VALUES ('lease', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (canonical({"owner": owner, "expires": now + ttl_s}),))
            return True
