"""Synthetic telemetry and fakes for fast, deterministic tests."""
from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

from demo.shop.controlplane import CATALOG
from lamplighter.actions import TransientError
from lamplighter.config import LOOPBACK_HOSTS, Settings, Timing

TIMING = Timing.demo()
T0 = 1_000_000.0  # arbitrary epoch for synthetic data
SLO = CATALOG["services"]["gateway"]["slo"]


def settings(tmp_path: Path, **overrides) -> Settings:
    base = Settings(data_dir=tmp_path, db_path=tmp_path / "db.sqlite", controlplane_url="http://127.0.0.1:1",
                    controlplane_token="x" * 24, allowed_hosts=LOOPBACK_HOSTS, timing=TIMING)
    return replace(base, **overrides)


class FakeTelemetry:
    def __init__(self) -> None:
        self._requests: dict[str, list[dict]] = {}
        self._calls: dict[str, list[dict]] = {}
        self._changes: list[dict] = []

    def request(self, service, ts, status=200, latency=12.0, error=None, release=None, req=None, resp=None):
        record = {"ts": ts, "kind": "request", "service": service, "method": "POST", "route": "/checkout",
                  "status": status, "latency_ms": latency, "trace_id": f"t{ts}", "release": release, "error": error}
        if req is not None:
            record.update(req=req, resp=resp)
        self._requests.setdefault(service, []).append(record)

    def call(self, service, target, ts, error=None, status=200, latency=5.0):
        self._calls.setdefault(service, []).append({"ts": ts, "kind": "call", "service": service, "target": target,
                                                    "status": status, "latency_ms": latency, "error": error})

    def change(self, change_id, ts, kind, service, old, new, key=None, actor="alice"):
        self._changes.append({"id": change_id, "ts": ts, "kind": kind, "service": service, "key": key,
                              "from": old, "to": new, "actor": actor})

    def requests(self, service, start, end):
        return [r for r in self._requests.get(service, []) if start <= r["ts"] < end]

    def calls(self, service, start, end):
        return [c for c in self._calls.get(service, []) if start <= c["ts"] < end]

    def changes(self, start, end):
        return [c for c in self._changes if start <= c["ts"] < end]

    def catalog(self):
        return copy.deepcopy(CATALOG)


def _ticks(start: float, end: float, rps: int = 10):
    count = int(round((end - start) * rps))
    return [start + i / rps for i in range(count)]


def healthy(tel: FakeTelemetry, start: float, end: float, release: str = "v1") -> None:
    for i, ts in enumerate(_ticks(start, end)):
        cart = {"items": [{"sku": f"SKU-{i % 8 + 1}", "qty": i % 3 + 1}]}
        tel.request("gateway", ts, req=cart, resp={"total_cents": 100 + i % 8, "order_id": f"o{i}"})
        tel.call("gateway", "checkout", ts)
        tel.request("checkout", ts, release=release)
        tel.call("checkout", "inventory", ts)
        tel.call("checkout", "payments", ts)
        tel.request("inventory", ts, latency=8.0)
        tel.request("payments", ts, latency=35.0)


def bad_release(tel: FakeTelemetry, start: float, end: float, release: str = "v2") -> None:
    error = {"type": "KeyError", "message": "'coupon'", "upstream": None,
             "stack": [{"file": "logic.py", "line": 34, "func": "_discount_percent", "in_release": True}]}
    for i, ts in enumerate(_ticks(start, end)):
        cart = {"items": [{"sku": f"SKU-{i % 8 + 1}", "qty": 1}]}
        tel.request("gateway", ts, status=500, req=cart, resp={"error": "internal"},
                    error={"type": "UpstreamError", "message": "checkout returned 500", "upstream": "checkout",
                           "upstream_kind": "http_5xx"})
        tel.call("gateway", "checkout", ts, error="http_5xx", status=500)
        tel.request("checkout", ts, status=500, release=release, error=error)
        tel.call("checkout", "inventory", ts)
        tel.request("inventory", ts, latency=8.0)  # the defect raises after pricing, before payment


def payments_timeouts(tel: FakeTelemetry, start: float, end: float, timeout_ms: int = 5) -> None:
    """Checkout gives up on payments too early; payments itself is healthy."""
    error = {"type": "UpstreamError", "message": f"payments timeout: after {timeout_ms}ms", "upstream": "payments",
             "upstream_kind": "timeout"}
    for i, ts in enumerate(_ticks(start, end)):
        cart = {"items": [{"sku": f"SKU-{i % 8 + 1}", "qty": 2}]}
        tel.request("gateway", ts, status=504, req=cart, resp={"error": "upstream failure"},
                    error={"type": "UpstreamError", "message": "checkout returned 504", "upstream": "checkout",
                           "upstream_kind": "http_5xx"})
        tel.call("gateway", "checkout", ts, error="http_5xx", status=504)
        tel.request("checkout", ts, status=504, release="v1", error=error)
        tel.call("checkout", "inventory", ts)
        tel.call("checkout", "payments", ts, error="timeout", status=0)
        tel.request("inventory", ts, latency=8.0)
        tel.request("payments", ts, latency=35.0)


def inventory_hang(tel: FakeTelemetry, start: float, end: float, release: str = "v1") -> None:
    """Inventory stops answering; checkout times out on it."""
    error = {"type": "UpstreamError", "message": "inventory timeout: after 800ms", "upstream": "inventory",
             "upstream_kind": "timeout"}
    for ts in _ticks(start, end):
        tel.request("gateway", ts, status=504, req={"items": [{"sku": "SKU-1", "qty": 1}]}, resp=None,
                    error={"type": "UpstreamError", "message": "checkout returned 504", "upstream": "checkout",
                           "upstream_kind": "http_5xx"})
        tel.call("gateway", "checkout", ts, error="http_5xx", status=504)
        tel.request("checkout", ts, status=504, release=release, error=error)
        tel.call("checkout", "inventory", ts, error="timeout", status=0)


class FakeActuator:
    """An in-memory control plane with the same idempotency contract as the real one."""

    def __init__(self, release="v2", config=None) -> None:
        self.releases = {"checkout": release}
        self.config = {"checkout": dict(config or {"payments.timeout_ms": 800})}
        self.applied: list[tuple] = []
        self._seen: dict[str, dict] = {}
        self.lose_next_response = False
        self.sandboxes = 0

    def state(self):
        return {"releases": dict(self.releases), "config": copy.deepcopy(self.config)}

    def _once(self, key, effect, record):
        if key in self._seen:
            return {**self._seen[key], "deduplicated": True}
        effect()
        self.applied.append(record)
        self._seen[key] = {"ok": True, "change": {"ts": 0.0}}
        if self.lose_next_response:
            self.lose_next_response = False
            raise TransientError("response lost after the change was applied")
        return self._seen[key]

    def deploy(self, service, release, key):
        return self._once(key, lambda: self.releases.__setitem__(service, release), ("deploy", service, release))

    def set_config(self, service, key_name, value, key):
        return self._once(key, lambda: self.config[service].__setitem__(key_name, value),
                          ("set_config", service, key_name, value))

    def restart(self, service, key):
        return self._once(key, lambda: None, ("restart", service))

    def sandbox_create(self, releases, config, override_files=None):
        self.sandboxes += 1
        return {"id": f"sb{self.sandboxes}", "checkout_url": "http://127.0.0.1:9"}

    def sandbox_delete(self, sandbox_id):
        return None


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now
