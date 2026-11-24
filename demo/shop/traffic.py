"""Edge gateway and synthetic customers for the demo shop.

The generator sends a steady, seeded stream of checkout requests and records
what the customer saw to logs/gateway.jsonl. That file is the edge telemetry
the SLO is measured on, and its captured request/response pairs are the
recorded traffic that proofs are replayed from.
"""
from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from demo.shop.common import LOCAL, JsonlLog, now

SKUS = [f"SKU-{i}" for i in range(1, 9)]
GATEWAY_TIMEOUT_S = 5.0


class TrafficGenerator:
    def __init__(self, checkout_url: str, log_dir: Path, rps: float = 20.0, seed: int = 7) -> None:
        self._url = f"{checkout_url}/checkout"
        self._log = JsonlLog(Path(log_dir) / "gateway.jsonl")
        self._interval = 1.0 / rps
        self._rng = random.Random(seed)
        self._pool = ThreadPoolExecutor(max_workers=128, thread_name_prefix="gw")
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="traffic")
        self.invalid_fraction = 0.04  # share of requests that are client mistakes

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._log.close()

    def _cart(self) -> dict:
        roll = self._rng.random()
        if roll < self.invalid_fraction / 2:
            return {"items": [{"sku": "SKU-404", "qty": 1}]}
        if roll < self.invalid_fraction:
            return {"items": [{"sku": self._rng.choice(SKUS), "qty": 0}]}
        count = self._rng.randint(1, 3)
        return {"items": [{"sku": sku, "qty": self._rng.randint(1, 4)} for sku in self._rng.sample(SKUS, count)]}

    def _run(self) -> None:
        next_at = time.monotonic()
        while not self._stop.is_set():
            self._pool.submit(self._one, self._cart())
            next_at += self._interval
            delay = next_at - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_at = time.monotonic()  # fell behind; do not burst to catch up

    def _one(self, cart: dict) -> None:
        trace_id = uuid.uuid4().hex
        request = urllib.request.Request(self._url, data=json.dumps(cart).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-Trace-Id": trace_id})
        t0 = time.perf_counter()
        status, payload, error = 0, None, None
        try:
            with LOCAL.open(request, timeout=GATEWAY_TIMEOUT_S) as resp:
                status, payload = resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                payload = json.loads(exc.read())
            except (json.JSONDecodeError, OSError):
                payload = None
            if status >= 500:
                error = {"type": "UpstreamError", "message": f"checkout returned {status}",
                         "upstream": "checkout", "upstream_kind": "http_5xx"}
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            timed_out = isinstance(reason, TimeoutError) or "timed out" in str(reason)
            status = 504 if timed_out else 503
            kind = "timeout" if timed_out else "unreachable"
            error = {"type": "UpstreamError", "message": f"checkout {kind}", "upstream": "checkout",
                     "upstream_kind": kind}
        except json.JSONDecodeError:
            status, error = 502, {"type": "UpstreamError", "message": "checkout sent invalid JSON",
                                  "upstream": "checkout", "upstream_kind": "bad_response"}
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        stamp = now()
        try:
            self._log.write({"ts": stamp, "kind": "call", "service": "gateway", "target": "checkout",
                             "status": status if error is None or error["upstream_kind"] == "http_5xx" else 0,
                             "latency_ms": latency_ms, "error": error["upstream_kind"] if error else None,
                             "trace_id": trace_id})
            self._log.write({"ts": stamp, "kind": "request", "service": "gateway", "method": "POST",
                             "route": "/checkout", "status": status, "latency_ms": latency_ms,
                             "trace_id": trace_id, "release": None, "error": error, "req": cart, "resp": payload})
        except ValueError:
            pass  # log closed during shutdown
