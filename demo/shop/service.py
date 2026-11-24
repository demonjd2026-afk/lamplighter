"""The demo shop: three small HTTP services in one module (standard library only).

    checkout  -> inventory (price lookup), payments (authorisation)

Each process writes structured JSON-lines telemetry to <log-dir>/<name>.jsonl:
  kind="request": one record per request served
  kind="call":    one record per outbound call
Run:  python -m demo.shop.service --name checkout --port 9101 ...
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import signal
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from demo.shop.common import LOCAL, JsonFile, JsonlLog, now

MAX_BODY = 64 * 1024
PRICES = {f"SKU-{i}": 1500 + 725 * i for i in range(1, 9)}
STUCK_SECONDS = 3.0


class ClientError(ValueError):
    """The request is invalid; maps to a 4xx response."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class UpstreamError(Exception):
    """A dependency failed or timed out."""

    def __init__(self, target: str, kind: str, detail: str) -> None:
        super().__init__(f"{target} {kind}: {detail}")
        self.target = target
        self.kind = kind


class Deps:
    """Outbound calls made by the checkout logic, with per-call telemetry."""

    def __init__(self, ctx: "Context", trace_id: str) -> None:
        self._ctx = ctx
        self._trace_id = trace_id

    def _call(self, target: str, url: str, payload: dict | None) -> tuple[int, Any]:
        cfg = self._ctx.config.get()
        timeout_ms = cfg.get(f"{target}.timeout_ms", 800)
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        req.add_header("X-Trace-Id", self._trace_id)
        if data:
            req.add_header("Content-Type", "application/json")
        t0 = time.perf_counter()
        status, error, body = 0, None, None
        try:
            with LOCAL.open(req, timeout=max(timeout_ms, 1) / 1000.0) as resp:
                status = resp.status
                body = json.loads(resp.read(MAX_BODY) or b"null")
        except urllib.error.HTTPError as exc:
            status = exc.code
            if status >= 500:
                error = "http_5xx"
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            error = "timeout" if isinstance(reason, TimeoutError) or "timed out" in str(reason) else "unreachable"
        except json.JSONDecodeError:
            error = "bad_response"
        latency_ms = (time.perf_counter() - t0) * 1000.0
        self._ctx.log.write({
            "ts": now(), "kind": "call", "service": self._ctx.name, "target": target,
            "status": status, "latency_ms": round(latency_ms, 2), "error": error,
            "timeout_ms": timeout_ms, "trace_id": self._trace_id,
        })
        if error:
            detail = f"after {timeout_ms}ms" if error == "timeout" else f"status {status}"
            raise UpstreamError(target, error, detail)
        return status, body

    def inventory_price(self, sku: str) -> int:
        status, body = self._call("inventory", f"{self._ctx.inventory_url}/price?sku={urllib.parse.quote(sku, safe='')}", None)
        if status == 404:
            raise ClientError(f"unknown sku {sku}", 422)
        return int(body["price_cents"])

    def authorize(self, amount_cents: int) -> dict:
        _, body = self._call("payments", f"{self._ctx.payments_url}/authorize", {"amount_cents": amount_cents})
        return body


class Context:
    def __init__(self, args: argparse.Namespace) -> None:
        self.name: str = args.name
        self.release: str = args.release
        self.log = JsonlLog(Path(args.log_dir) / f"{args.name}.jsonl")
        self.config = JsonFile(Path(args.config_file), {}) if args.config_file else JsonFile(Path("/nonexistent"), {})
        self.fault = JsonFile(Path(args.fault_file), {}) if args.fault_file else JsonFile(Path("/nonexistent"), {})
        self.inventory_url: str = args.inventory_url
        self.payments_url: str = args.payments_url
        self.release_dir = Path(args.release_dir).resolve() if args.release_dir else None
        self.logic = None
        if self.name == "checkout":
            if self.release_dir is None:
                raise SystemExit("checkout needs --release-dir")
            spec = importlib.util.spec_from_file_location("checkout_logic", self.release_dir / "logic.py")
            if spec is None or spec.loader is None:
                raise SystemExit("cannot load release")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.logic = module

    def active_fault(self) -> str:
        fault = self.fault.get() or {}
        until = fault.get("until")
        if until is not None and now() > until:
            return "none"
        return fault.get("mode", "none")

    def frames(self, exc: BaseException) -> list[dict]:
        out = []
        for frame in traceback.extract_tb(exc.__traceback__):
            path = Path(frame.filename).resolve()
            in_release = self.release_dir is not None and self.release_dir in path.parents
            out.append({
                "file": str(path.relative_to(self.release_dir)) if in_release else path.name,
                "line": frame.lineno, "func": frame.name, "in_release": in_release,
            })
        return out


def make_handler(ctx: Context) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "shop/1"

        def log_message(self, *_args: Any) -> None:  # silence default access log
            return

        def _send(self, status: int, payload: Any) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the caller gave up (timeout); nothing to do

        def _read_json(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ClientError("body too large", 413)
            raw = self.rfile.read(length) if length else b""
            try:
                return json.loads(raw) if raw else None
            except json.JSONDecodeError:
                raise ClientError("body is not valid JSON") from None

        def _serve(self, method: str) -> None:
            t0 = time.perf_counter()
            trace_id = self.headers.get("X-Trace-Id") or uuid.uuid4().hex
            route = self.path.split("?", 1)[0]
            status, payload, error = 500, {"error": "internal"}, None
            try:
                status, payload = self._route(method, route, trace_id)
            except ClientError as exc:
                status, payload = exc.status, {"error": str(exc)}
            except UpstreamError as exc:
                status = 504 if exc.kind == "timeout" else 502
                payload = {"error": "upstream failure"}
                error = {"type": "UpstreamError", "message": str(exc), "upstream": exc.target, "upstream_kind": exc.kind}
            except Exception as exc:  # noqa: BLE001 - the service boundary
                if exc.__class__.__name__ == "BadRequest":
                    status, payload = 400, {"error": str(exc)}
                else:
                    status, payload = 500, {"error": "internal"}
                    error = {"type": exc.__class__.__name__, "message": str(exc), "upstream": None,
                             "stack": ctx.frames(exc)}
            self._send(status, payload)
            ctx.log.write({
                "ts": now(), "kind": "request", "service": ctx.name, "method": method, "route": route,
                "status": status, "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                "trace_id": trace_id, "release": ctx.release, "error": error,
            })

        def _route(self, method: str, route: str, trace_id: str) -> tuple[int, Any]:
            if method == "GET" and route == "/healthz":
                return 200, {"ok": True, "service": ctx.name, "release": ctx.release}
            fault = ctx.active_fault()
            if ctx.name == "checkout" and method == "POST" and route == "/checkout":
                body = self._read_json()
                result = ctx.logic.handle_checkout(body, Deps(ctx, trace_id), ctx.config.get())
                result["order_id"] = uuid.uuid4().hex
                return 200, result
            if ctx.name == "inventory" and method == "GET" and route == "/price":
                if fault == "stuck":
                    time.sleep(STUCK_SECONDS)
                sku = urllib.parse.unquote(self.path.partition("sku=")[2].split("&", 1)[0])
                if sku not in PRICES:
                    raise ClientError("unknown sku", 404)
                return 200, {"sku": sku, "price_cents": PRICES[sku]}
            if ctx.name == "payments" and method == "POST" and route == "/authorize":
                body = self._read_json()
                if fault == "errors":
                    raise RuntimeError("payment processor unavailable")
                amount = body.get("amount_cents") if isinstance(body, dict) else None
                if not isinstance(amount, int) or amount < 0:
                    raise ClientError("amount_cents must be a non-negative integer")
                time.sleep(0.03)
                return 200, {"approved": True, "auth_id": uuid.uuid4().hex}
            raise ClientError("not found", 404)

        def do_GET(self) -> None:  # noqa: N802
            self._serve("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._serve("POST")

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, choices=["checkout", "inventory", "payments"])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--log-dir", required=True)
    parser.add_argument("--release", default="v1")
    parser.add_argument("--release-dir", default="")
    parser.add_argument("--config-file", default="")
    parser.add_argument("--fault-file", default="")
    parser.add_argument("--inventory-url", default="")
    parser.add_argument("--payments-url", default="")
    args = parser.parse_args(argv)

    ctx = Context(args)
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer.request_queue_size = 128
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(ctx))

    def stop(*_sig: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        ctx.log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
