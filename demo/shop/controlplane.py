"""Deploy control plane for the demo shop.

It plays the role a real platform plays (Kubernetes, a deploy service, a config
service): it owns the processes, the release pointer and the configuration, it
records every change as a change event, and it offers an authenticated HTTP API
that the on-call automation uses to act. Mutations are idempotent by key.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from demo.shop.common import LOCAL, JsonlLog, atomic_write_json, now

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RELEASES_ROOT = Path(__file__).resolve().parent / "releases"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")
CONFIG_SCHEMA = {  # key -> (min, max); anything else is rejected
    "inventory.timeout_ms": (1, 10_000),
    "payments.timeout_ms": (1, 10_000),
}
DEFAULT_CONFIG = {"inventory.timeout_ms": 800, "payments.timeout_ms": 800}
CATALOG = {
    "services": {
        "gateway": {"deps": ["checkout"], "edge": True, "managed": False,
                    "slo": {"target": 0.99, "latency_ms": 2000}},
        "checkout": {"deps": ["inventory", "payments"], "managed": True, "restart_safe": True,
                     "has_releases": True, "has_config": True},
        "inventory": {"deps": [], "managed": True, "restart_safe": True},
        "payments": {"deps": [], "managed": False, "external": True},
    }
}
SERVICES = ("checkout", "inventory", "payments")
MAX_SANDBOXES = 2
SANDBOX_TTL_S = 120.0
MAX_BODY = 256 * 1024


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_healthy(url: str, timeout_s: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with LOCAL.open(f"{url}/healthz", timeout=0.5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.05)
    return False


class Stack:
    """One running set of shop processes (the live stack or a sandbox)."""

    def __init__(self, data_dir: Path, ports: dict[str, int]) -> None:
        self.data_dir = data_dir
        self.ports = ports
        self.procs: dict[str, subprocess.Popen] = {}
        self.release_dirs: dict[str, Path] = {}

    def url(self, service: str) -> str:
        return f"http://127.0.0.1:{self.ports[service]}"

    def config_file(self, service: str) -> Path:
        return self.data_dir / "config" / f"{service}.json"

    def fault_file(self, service: str) -> Path:
        return self.data_dir / "faults" / f"{service}.json"

    def spawn(self, service: str, release: str = "v1") -> None:
        cmd = [sys.executable, "-m", "demo.shop.service", "--name", service,
               "--port", str(self.ports[service]), "--log-dir", str(self.data_dir / "logs"),
               "--fault-file", str(self.fault_file(service))]
        if service == "checkout":
            cmd += ["--release", release, "--release-dir", str(self.release_dirs["checkout"]),
                    "--config-file", str(self.config_file("checkout")),
                    "--inventory-url", self.url("inventory"), "--payments-url", self.url("payments")]
        env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
        self.stderr_file(service).parent.mkdir(parents=True, exist_ok=True)
        with open(self.stderr_file(service), "ab") as errors:
            self.procs[service] = subprocess.Popen(cmd, cwd=PROJECT_ROOT, env=env,
                                                   stdout=subprocess.DEVNULL, stderr=errors)

    def stderr_file(self, service: str) -> Path:
        return self.data_dir / "stderr" / f"{service}.log"

    def why_unhealthy(self, service: str) -> str:
        """A short explanation for a service that did not come up: exit status and the end of its stderr."""
        proc = self.procs.get(service)
        status = "not started" if proc is None else (
            "still running but not answering /healthz" if proc.poll() is None else f"exited with status {proc.returncode}")
        try:
            tail = self.stderr_file(service).read_text(encoding="utf-8", errors="replace").strip().splitlines()[-6:]
        except OSError:
            tail = []
        return f"{service} did not become healthy ({status})" + ("; stderr: " + " | ".join(tail) if tail else "")

    def stop(self, service: str, hard: bool = False) -> None:
        proc = self.procs.pop(service, None)
        if proc is None or proc.poll() is not None:
            return
        proc.send_signal(signal.SIGKILL if hard else signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)

    def stop_all(self) -> None:
        for service in list(self.procs):
            self.stop(service)


class ControlPlane:
    def __init__(self, data_dir: Path, base_port: int, token: str) -> None:
        if len(token) < 16:
            raise ValueError("control plane token must be at least 16 characters")
        self.data_dir = Path(data_dir)
        self.token = token
        self.base_port = base_port
        self.live = Stack(self.data_dir, {"checkout": base_port + 1, "inventory": base_port + 2,
                                          "payments": base_port + 3})
        self._lock = threading.RLock()
        self._state_file = self.data_dir / "controlplane" / "state.json"
        self._changes = JsonlLog(self.data_dir / "changes.jsonl")
        self._sandboxes: dict[str, tuple[Stack, Path, float]] = {}
        self._server: ThreadingHTTPServer | None = None
        self.state: dict[str, Any] = {"releases": {"checkout": "v1"}, "config": {"checkout": dict(DEFAULT_CONFIG)},
                                      "idempotency": {}, "seq": 0}
        if self._state_file.exists():
            self.state = json.loads(self._state_file.read_text(encoding="utf-8"))

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            atomic_write_json(self.data_dir / "catalog.json", CATALOG)
            atomic_write_json(self.live.config_file("checkout"), self.state["config"]["checkout"])
            self.live.release_dirs["checkout"] = self._release_dir("checkout", self.state["releases"]["checkout"])
            for service in ("inventory", "payments", "checkout"):
                self.live.spawn(service, self.state["releases"].get(service, "v1"))
            for service in SERVICES:
                if not wait_healthy(self.live.url(service)):
                    raise RuntimeError(self.live.why_unhealthy(service))
            self._save()

    def stop(self) -> None:
        with self._lock:
            if self._server is not None:
                self._server.shutdown()
                self._server.server_close()
                self._server = None
            for sandbox_id in list(self._sandboxes):
                self.sandbox_delete(sandbox_id)
            self.live.stop_all()
            self._changes.close()

    # ---- helpers ---------------------------------------------------------
    def _save(self) -> None:
        atomic_write_json(self._state_file, self.state)

    def _release_dir(self, service: str, release: str) -> Path:
        if not NAME_RE.match(service) or not NAME_RE.match(release):
            raise ValueError("invalid service or release name")
        path = (RELEASES_ROOT / service / release).resolve()
        if RELEASES_ROOT.resolve() not in path.parents or not (path / "logic.py").is_file():
            raise ValueError(f"unknown release {service}/{release}")
        return path

    def _record_change(self, kind: str, service: str, old: Any, new: Any, actor: str, key: str | None = None) -> dict:
        self.state["seq"] += 1
        change = {"id": f"chg-{self.state['seq']:04d}", "ts": now(), "kind": kind, "service": service,
                  "key": key, "from": old, "to": new, "actor": str(actor)[:64]}
        self._changes.write(change)
        return change

    def _idempotent(self, key: str | None) -> dict | None:
        if key is None:
            return None
        if not isinstance(key, str) or not 8 <= len(key) <= 128:
            raise ValueError("idempotency_key must be 8 to 128 characters")
        return self.state["idempotency"].get(key)

    def _remember(self, key: str | None, result: dict) -> dict:
        if key is not None:
            self.state["idempotency"][key] = result
        self._save()
        return result

    def _require_managed(self, service: str) -> dict:
        meta = CATALOG["services"].get(service)
        if meta is None:
            raise ValueError(f"unknown service {service}")
        if not meta.get("managed"):
            raise PermissionError(f"{service} is not managed by this control plane")
        return meta

    # ---- mutations (all idempotent by key) -------------------------------
    def deploy(self, service: str, release: str, idempotency_key: str | None = None, actor: str = "human") -> dict:
        with self._lock:
            if (done := self._idempotent(idempotency_key)) is not None:
                return {**done, "deduplicated": True}
            meta = self._require_managed(service)
            if not meta.get("has_releases"):
                raise ValueError(f"{service} has no release registry")
            new_dir = self._release_dir(service, release)
            old = self.state["releases"][service]
            self.state["releases"][service] = release
            change = self._record_change("deploy", service, old, release, actor)
            self.live.stop(service)
            self.live.release_dirs[service] = new_dir
            self.live.spawn(service, release)
            healthy = wait_healthy(self.live.url(service))
            return self._remember(idempotency_key, {"ok": healthy, "change": change})

    def set_config(self, service: str, key: str, value: Any, idempotency_key: str | None = None,
                   actor: str = "human") -> dict:
        with self._lock:
            if (done := self._idempotent(idempotency_key)) is not None:
                return {**done, "deduplicated": True}
            meta = self._require_managed(service)
            if not meta.get("has_config") or key not in CONFIG_SCHEMA:
                raise ValueError(f"unknown config key {service}/{key}")
            low, high = CONFIG_SCHEMA[key]
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise ValueError(f"{key} must be an integer from {low} to {high}")
            old = self.state["config"][service].get(key)
            self.state["config"][service][key] = value
            atomic_write_json(self.live.config_file(service), self.state["config"][service])
            change = self._record_change("config", service, old, value, actor, key=key)
            return self._remember(idempotency_key, {"ok": True, "change": change})

    def restart(self, service: str, idempotency_key: str | None = None, actor: str = "human") -> dict:
        with self._lock:
            if (done := self._idempotent(idempotency_key)) is not None:
                return {**done, "deduplicated": True}
            meta = self._require_managed(service)
            if not meta.get("restart_safe"):
                raise PermissionError(f"{service} is not marked restart_safe")
            fault_file = self.live.fault_file(service)
            if fault_file.exists():
                fault = json.loads(fault_file.read_text(encoding="utf-8"))
                if not fault.get("persistent"):
                    fault_file.unlink()  # in-memory state is lost on restart
            change = self._record_change("restart", service, None, None, actor)
            self.live.stop(service)
            self.live.spawn(service, self.state["releases"].get(service, "v1"))
            healthy = wait_healthy(self.live.url(service))
            return self._remember(idempotency_key, {"ok": healthy, "change": change})

    # ---- chaos (demo only; deliberately NOT recorded as change events) ----
    def inject_fault(self, service: str, mode: str, persistent: bool = False, ttl_s: float | None = None) -> dict:
        with self._lock:
            if service not in SERVICES or mode not in ("none", "stuck", "errors"):
                raise ValueError("unknown service or fault mode")
            fault = {"mode": mode, "persistent": bool(persistent), "until": now() + ttl_s if ttl_s else None}
            atomic_write_json(self.live.fault_file(service), fault)
            return {"ok": True, "fault": fault}

    def kill(self, service: str) -> dict:
        with self._lock:
            if service not in SERVICES:
                raise ValueError("unknown service")
            self.live.stop(service, hard=True)
            return {"ok": True}

    # ---- sandboxes --------------------------------------------------------
    def sandbox_create(self, releases: dict | None = None, config: dict | None = None,
                       override_files: dict | None = None) -> dict:
        with self._lock:
            self._reap_sandboxes()
            if len(self._sandboxes) >= MAX_SANDBOXES:
                raise RuntimeError("too many sandboxes")
            release = (releases or {}).get("checkout", self.state["releases"]["checkout"])
            source = self._release_dir("checkout", release)
            cfg = dict(self.state["config"]["checkout"])
            for key, value in (config or {}).items():
                if key not in CONFIG_SCHEMA:
                    raise ValueError(f"unknown config key {key}")
                low, high = CONFIG_SCHEMA[key]
                if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                    raise ValueError(f"{key} out of range")
                cfg[key] = value
            root = Path(tempfile.mkdtemp(prefix="sandbox-", dir=self.data_dir / "controlplane"))
            release_copy = root / "release"
            shutil.copytree(source, release_copy)
            for name, content in (override_files or {}).items():
                target = release_copy / name
                if Path(name).name != name or not target.is_file():
                    shutil.rmtree(root, ignore_errors=True)
                    raise ValueError("override must replace an existing file of the release")
                if not isinstance(content, str) or len(content.encode()) > 64 * 1024:
                    shutil.rmtree(root, ignore_errors=True)
                    raise ValueError("override content too large")
                target.write_text(content, encoding="utf-8")
            stack = Stack(root, {name: free_port() for name in SERVICES})
            stack.release_dirs["checkout"] = release_copy
            atomic_write_json(stack.config_file("checkout"), cfg)
            for service in ("inventory", "payments", "checkout"):
                stack.spawn(service, f"sandbox:{release}")
            if not all(wait_healthy(stack.url(service)) for service in SERVICES):
                stack.stop_all()
                shutil.rmtree(root, ignore_errors=True)
                raise RuntimeError("sandbox did not become healthy")
            sandbox_id = uuid.uuid4().hex[:12]
            self._sandboxes[sandbox_id] = (stack, root, time.monotonic())
            return {"id": sandbox_id, "checkout_url": stack.url("checkout"), "release": release, "config": cfg}

    def sandbox_delete(self, sandbox_id: str) -> dict:
        with self._lock:
            entry = self._sandboxes.pop(sandbox_id, None)
            if entry is not None:
                entry[0].stop_all()
                shutil.rmtree(entry[1], ignore_errors=True)
            return {"ok": True}

    def _reap_sandboxes(self) -> None:
        for sandbox_id, (_, _, born) in list(self._sandboxes.items()):
            if time.monotonic() - born > SANDBOX_TTL_S:
                self.sandbox_delete(sandbox_id)

    def describe(self) -> dict:
        with self._lock:
            return {"releases": dict(self.state["releases"]), "config": json.loads(json.dumps(self.state["config"])),
                    "processes": {name: (proc.poll() is None) for name, proc in self.live.procs.items()},
                    "urls": {name: self.live.url(name) for name in SERVICES}}

    # ---- HTTP API ---------------------------------------------------------
    def serve(self) -> None:
        plane = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def _reply(self, status: int, payload: Any) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorised(self) -> bool:
                supplied = self.headers.get("Authorization", "")
                return hmac.compare_digest(supplied.encode(), f"Bearer {plane.token}".encode())

            def _handle(self, method: str) -> None:
                if not self._authorised():
                    self._reply(401, {"error": "unauthorised"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > MAX_BODY:
                        raise ValueError("body too large")
                    body = json.loads(self.rfile.read(length)) if length else {}
                    if not isinstance(body, dict):
                        raise ValueError("body must be a JSON object")
                    self._reply(200, self._dispatch(method, self.path, body))
                except PermissionError as exc:
                    self._reply(403, {"error": str(exc)})
                except (ValueError, KeyError, TypeError) as exc:
                    self._reply(400, {"error": str(exc)})
                except RuntimeError as exc:
                    self._reply(503, {"error": str(exc)})

            def _dispatch(self, method: str, path: str, body: dict) -> dict:
                actor = body.get("actor", "api")
                key = body.get("idempotency_key")
                if method == "GET" and path == "/state":
                    return plane.describe()
                if method == "POST" and path == "/deploy":
                    return plane.deploy(body["service"], body["release"], key, actor)
                if method == "POST" and path == "/config":
                    return plane.set_config(body["service"], body["key"], body["value"], key, actor)
                if method == "POST" and path == "/restart":
                    return plane.restart(body["service"], key, actor)
                if method == "POST" and path == "/sandbox":
                    return plane.sandbox_create(body.get("releases"), body.get("config"), body.get("override_files"))
                if method == "DELETE" and path.startswith("/sandbox/"):
                    return plane.sandbox_delete(path.rsplit("/", 1)[1])
                raise ValueError("unknown route")

            def do_GET(self) -> None:  # noqa: N802
                self._handle("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._handle("POST")

            def do_DELETE(self) -> None:  # noqa: N802
                self._handle("DELETE")

        ThreadingHTTPServer.daemon_threads = True
        self._server = ThreadingHTTPServer(("127.0.0.1", self.base_port), Handler)
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.base_port}"
