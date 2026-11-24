"""A self-contained lab: the demo shop, its traffic, and Lamplighter watching it.

Used by the evaluation, the end-to-end tests and the demo. Everything runs as
real local processes and real HTTP; nothing is mocked.
"""
from __future__ import annotations

import json
import random
import secrets
import socket
import threading
import time
from dataclasses import replace
from pathlib import Path

from demo.shop.controlplane import RELEASES_ROOT, ControlPlane
from demo.shop.traffic import TrafficGenerator
from lamplighter.cli import Runtime, build
from lamplighter.config import LOOPBACK_HOSTS, Settings, Timing
from lamplighter.llm import LLMClient
from lamplighter.store import TERMINAL_STATES

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def pick_base_port(count: int = 4) -> int:
    rng = random.Random()
    for _ in range(200):
        base = rng.randrange(20000, 60000, 10)
        sockets = []
        try:
            for offset in range(count):
                sock = socket.socket()
                sock.bind(("127.0.0.1", base + offset))
                sockets.append(sock)
            return base
        except OSError:
            continue
        finally:
            for sock in sockets:
                sock.close()
    raise RuntimeError("no free port range")


class Lab:
    def __init__(self, data_dir: Path, seed: int = 7, rps: float = 20.0, policy_file: Path | None = None,
                 llm: LLMClient | None = None, timing: Timing | None = None, llm_patch: bool = False,
                 api_token: str = "", public_read: bool = False) -> None:
        self.data_dir = Path(data_dir)
        self.cp_token = secrets.token_urlsafe(24)
        self.plane = ControlPlane(self.data_dir, pick_base_port(), self.cp_token)
        self.settings = Settings(
            data_dir=self.data_dir, db_path=self.data_dir / "lamplighter.db", controlplane_url=self.plane.url,
            controlplane_token=self.cp_token, api_token=api_token, public_read=public_read,
            policy_file=policy_file or PROJECT_ROOT / "demo" / "policy.toml", releases_root=RELEASES_ROOT,
            allowed_hosts=LOOPBACK_HOSTS, llm_patch=llm_patch, timing=timing or Timing.demo())
        self._seed, self._rps, self._llm = seed, rps, llm
        self.traffic: TrafficGenerator | None = None
        self.runtime: Runtime | None = None
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None

    def start(self, warmup_s: float = 15.0, worker: bool = True) -> "Lab":
        self.plane.start()
        self.plane.serve()
        self.traffic = TrafficGenerator(self.plane.live.url("checkout"), self.data_dir / "logs", self._rps, self._seed)
        self.traffic.start()
        self.runtime = build(self.settings, self._llm)
        if worker:
            self.start_worker()
        time.sleep(warmup_s)
        return self

    def start_worker(self) -> None:
        self._stop.clear()
        self._worker = threading.Thread(target=self.runtime.orchestrator.run, args=(self._stop, 0.5), daemon=True)
        self._worker.start()

    def stop_worker(self) -> None:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=30)
            self._worker = None

    def restart_runtime(self) -> None:
        """Simulate a crash and restart of Lamplighter itself: new process state, same database."""
        self.stop_worker()
        self.runtime.store.close()
        self.runtime = build(self.settings, self._llm)

    def stop(self) -> None:
        self.stop_worker()
        if self.traffic is not None:
            self.traffic.stop()
        self.plane.stop()
        if self.runtime is not None:
            self.runtime.store.close()

    # ---- observation -----------------------------------------------------
    def incidents(self) -> list[dict]:
        return list(reversed(self.runtime.store.list_incidents(limit=50)))

    def wait_closed(self, timeout_s: float = 100.0) -> dict | None:
        """Wait until an incident exists and is closed. Returns it, or None on timeout."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            found = self.incidents()
            if found and found[-1]["state"] in TERMINAL_STATES:
                return self._after_post_close(found[-1])
            time.sleep(0.25)
        found = self.incidents()
        return found[-1] if found else None

    def _after_post_close(self, incident: dict, timeout_s: float = 30.0) -> dict:
        """Closing is durable first; the postmortem and briefs are written just after. Wait for them."""
        report = self.data_dir / "incidents" / incident["id"] / "postmortem.md"
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and not report.is_file():
            time.sleep(0.1)
        return self.runtime.store.get_incident(incident["id"])

    def wait_state(self, state: str, timeout_s: float = 60.0) -> dict | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            found = self.incidents()
            if found and found[-1]["state"] == state:
                return found[-1]
            time.sleep(0.1)
        return None

    def gateway_records(self) -> list[dict]:
        path = self.data_dir / "logs" / "gateway.jsonl"
        records = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("kind") == "request":
                    records.append(record)
        return records

    def customer_impact(self, since: float) -> dict:
        """What customers saw from `since` on: failed requests, and when the last failure happened."""
        records = [r for r in self.gateway_records() if r["ts"] >= since]
        failed = [r for r in records if r["status"] >= 500]
        return {"requests": len(records), "failed": len(failed),
                "impact_s": round(max(r["ts"] for r in failed) - since, 2) if failed else 0.0}


def with_timing(lab_timing: Timing, **changes: float) -> Timing:
    return replace(lab_timing, **changes)
