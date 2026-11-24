"""HTTP API and dashboard.

Read routes need the API token unless LAMPLIGHTER_PUBLIC_READ is set. Decisions
always need the token. The alert webhook needs an HMAC signature. Routes whose
secret is not configured are disabled rather than open.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .config import Settings
from .detect import evaluate, is_bad, slo_services
from .evidence import p95
from .orchestrator import Orchestrator
from .redact import redact
from .store import OPEN_STATES, TERMINAL_STATES, Store
from .telemetry import TelemetrySource

STATIC = Path(__file__).parent / "static"
INCIDENT_ID = re.compile(r"^inc-\d{8}-\d{6}-[0-9a-f]{6}$")
MAX_WEBHOOK_BYTES = 16 * 1024
WEBHOOK_TOLERANCE_S = 300
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; font-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"


class Decision(BaseModel):
    decision: Literal["approved", "rejected"]
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor: str = Field(min_length=1, max_length=64, pattern=r"^[\w .@-]+$")


class ExternalAlert(BaseModel):
    service: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    source: str = Field(default="webhook", max_length=64, pattern=r"^[\w .:-]*$")


def _summary(incident: dict) -> dict:
    data = incident["data"]
    diagnosis = data.get("diagnosis") or {}
    accepted = next((h for h in diagnosis.get("hypotheses", []) if h["id"] == diagnosis.get("accepted")), None)
    return {"id": incident["id"], "service": incident["service"], "state": incident["state"],
            "opened_at": incident["opened_at"], "closed_at": incident["closed_at"],
            "summary": accepted["summary"] if accepted else None,
            "reason": (data.get("outcome") or {}).get("reason"),
            "plan": data.get("plan"), "plan_hash": data.get("plan_hash")}


def _slow(recent: list[dict], earlier: list[dict]) -> bool:
    if len(recent) < 3 or len(earlier) < 10:
        return False
    now_p95, then_p95 = p95([r["latency_ms"] for r in recent]), p95([r["latency_ms"] for r in earlier])
    return now_p95 >= max(3 * then_p95, then_p95 + 200.0)


def create_app(settings: Settings, store: Store, telemetry: TelemetrySource, orchestrator: Orchestrator) -> FastAPI:
    app = FastAPI(title="Lamplighter", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def _token_ok(request: Request) -> bool:
        supplied = request.headers.get("authorization", "")
        return bool(settings.api_token) and hmac.compare_digest(supplied.encode(),
                                                                f"Bearer {settings.api_token}".encode())

    def can_read(request: Request) -> None:
        if settings.public_read or _token_ok(request):
            return
        raise HTTPException(401, "a valid API token is required", headers={"WWW-Authenticate": "Bearer"})

    def can_write(request: Request) -> None:
        if not settings.api_token:
            raise HTTPException(503, "decisions are disabled: no API token is configured")
        if not _token_ok(request):
            raise HTTPException(401, "a valid API token is required", headers={"WWW-Authenticate": "Bearer"})

    def _incident(incident_id: str) -> dict:
        if not INCIDENT_ID.match(incident_id):
            raise HTTPException(404, "no such incident")
        incident = store.get_incident(incident_id)
        if incident is None:
            raise HTTPException(404, "no such incident")
        return incident

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/metrics", dependencies=[Depends(can_read)])
    def metrics() -> PlainTextResponse:
        incidents = store.list_incidents(limit=500)
        lines = ["# TYPE lamplighter_incidents gauge"]
        for state in (*OPEN_STATES, *TERMINAL_STATES):
            count = sum(1 for i in incidents if i["state"] == state)
            lines.append(f'lamplighter_incidents{{state="{state}"}} {count}')
        closed = [i["data"]["outcome"]["time_to_close_s"] for i in incidents
                  if i["state"] == "RESOLVED" and (i["data"].get("outcome") or {}).get("time_to_close_s") is not None]
        lines += ["# TYPE lamplighter_resolved_time_to_close_seconds_sum counter",
                  f"lamplighter_resolved_time_to_close_seconds_sum {sum(closed):.3f}",
                  "# TYPE lamplighter_resolved_time_to_close_seconds_count counter",
                  f"lamplighter_resolved_time_to_close_seconds_count {len(closed)}",
                  "# TYPE lamplighter_actions_last_hour gauge",
                  f"lamplighter_actions_last_hour {store.actions_since(time.time() - 3600)}"]
        skipped = getattr(telemetry, "skipped_lines", lambda: 0)()
        lines += ["# TYPE lamplighter_telemetry_skipped_lines counter",
                  f"lamplighter_telemetry_skipped_lines {skipped}"]
        return PlainTextResponse("\n".join(lines) + "\n")

    @app.get("/v1/status", dependencies=[Depends(can_read)])
    def status() -> dict:
        now, timing = time.time(), settings.timing
        out = {}
        for service, slo in slo_services(telemetry.catalog()).items():
            buckets = 60
            start = now - buckets * timing.bucket_s
            series = [[0, 0] for _ in range(buckets)]
            for request in telemetry.requests(service, start, now):
                index = min(buckets - 1, int((request["ts"] - start) / timing.bucket_s))
                series[index][0] += 1
                series[index][1] += is_bad(request, slo)
            alert = evaluate(telemetry, service, slo, now, timing)
            out[service] = {"slo": slo, "firing": alert is not None, "bucket_s": timing.bucket_s, "series": series,
                            "burn_short": alert["burn_short"] if alert else None}
        # live picture of every service and call path, for the dashboard's street view
        span = max(timing.recent_window_s * 2, 5 * timing.bucket_s)
        catalog = telemetry.catalog().get("services", {})
        edges, inbound = [], {}
        for name, meta in catalog.items():
            calls = telemetry.calls(name, now - span, now)
            for dep in meta.get("deps", []):
                if dep not in catalog:
                    continue
                to_dep = [c for c in calls if c.get("target") == dep]
                failed = sum(1 for c in to_dep if c.get("error"))
                edges.append({"from": name, "to": dep, "calls": len(to_dep), "failed": failed})
                totals = inbound.setdefault(dep, [0, 0])
                totals[0] += len(to_dep)
                totals[1] += failed
        nodes = {}
        for name, meta in catalog.items():
            requests = telemetry.requests(name, now - span, now)
            slo = meta.get("slo")
            bad = sum(1 for r in requests if (is_bad(r, slo) if slo else r["status"] >= 500))
            attempts, failed = inbound.get(name, [0, 0])
            if attempts >= 5 and failed / attempts >= 0.5 and len(requests) < 0.2 * attempts:
                state = "dark"          # callers keep trying and failing; it serves almost nothing
            elif requests and bad / len(requests) >= 0.05:
                state = "failing"
            elif _slow(requests, telemetry.requests(name, now - 8 * span, now - 2 * span)):
                state = "failing"       # answering, but far slower than it was a moment ago
            else:
                state = "ok"
            nodes[name] = {"deps": [d for d in meta.get("deps", []) if d in catalog], "requests": len(requests),
                           "bad": bad, "per_s": round(len(requests) / span, 1), "state": state,
                           "external": bool(meta.get("external")), "edge": bool(meta.get("edge"))}
        return {"now": now, "services": out, "topology": {"span_s": span, "services": nodes, "edges": edges},
                "mode": orchestrator.policy.mode,
                "kill_switch": orchestrator.policy.kill_switch_on(), "model": bool(orchestrator.llm)}

    @app.get("/v1/incidents", dependencies=[Depends(can_read)])
    def incidents(limit: int = 50) -> dict:
        return {"incidents": [_summary(i) for i in store.list_incidents(limit=limit)]}

    @app.get("/v1/incidents/{incident_id}", dependencies=[Depends(can_read)])
    def incident(incident_id: str) -> JSONResponse:
        found = _incident(incident_id)
        return JSONResponse({**found, "data": redact(found["data"]), "actions": redact(store.actions_for(incident_id)),
                             "audit": [{"seq": e["seq"], "ts": e["ts"], "event": e["event"], "hash": e["hash"]}
                                       for e in store.audit_trail(incident_id)]})

    @app.get("/v1/incidents/{incident_id}/postmortem", dependencies=[Depends(can_read)])
    def postmortem(incident_id: str) -> PlainTextResponse:
        found = _incident(incident_id)
        path = settings.data_dir / "incidents" / found["id"] / "postmortem.md"
        if not path.is_file():
            raise HTTPException(404, "the postmortem is not written yet")
        return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/markdown")

    @app.post("/v1/incidents/{incident_id}/decision", dependencies=[Depends(can_write)])
    def decide(incident_id: str, body: Decision) -> dict:
        found = _incident(incident_id)
        if not store.decide(found["id"], body.decision, body.actor, body.plan_hash, time.time()):
            raise HTTPException(409, "this incident is not awaiting a decision on that plan")
        return {"ok": True}

    @app.post("/v1/alerts")
    async def external_alert(request: Request) -> dict:
        if not settings.webhook_secret:
            raise HTTPException(503, "the alert webhook is disabled: no secret is configured")
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_WEBHOOK_BYTES:
            raise HTTPException(413, "body too large")
        raw = b""
        async for chunk in request.stream():   # never buffer more than the cap, whatever the header said
            raw += chunk
            if len(raw) > MAX_WEBHOOK_BYTES:
                raise HTTPException(413, "body too large")
        stamp = request.headers.get("x-lamplighter-timestamp", "")
        signature = request.headers.get("x-lamplighter-signature", "")
        if not stamp.isdigit() or abs(time.time() - int(stamp)) > WEBHOOK_TOLERANCE_S:
            raise HTTPException(401, "missing or stale timestamp")
        expected = "sha256=" + hmac.new(settings.webhook_secret.encode(), stamp.encode() + b"." + raw,
                                        hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature.encode(), expected.encode()):
            raise HTTPException(401, "bad signature")
        try:
            alert = ExternalAlert.model_validate_json(raw)
        except ValueError:
            raise HTTPException(422, "invalid alert body") from None
        opened = orchestrator.open_external(alert.service, alert.source)
        return {"ok": True, "incident": opened["id"] if opened else None}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/static/{name}")
    def static(name: str) -> Response:
        allowed = {"app.bundle.js": "text/javascript", "style.css": "text/css", "icon-calm.svg": "image/svg+xml",
                   "icon-alarm.svg": "image/svg+xml", "icon-work.svg": "image/svg+xml"}
        if name not in allowed:
            raise HTTPException(404, "not found")
        return FileResponse(STATIC / name, media_type=allowed[name])

    @app.get("/static/fonts/{name}")
    def font(name: str) -> Response:
        if name not in ("chakra-petch-latin-600-normal.woff2", "chakra-petch-latin-700-normal.woff2"):
            raise HTTPException(404, "not found")
        return FileResponse(STATIC / "fonts" / name, media_type="font/woff2")

    return app
