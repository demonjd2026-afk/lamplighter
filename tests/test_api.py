import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from lamplighter.api import create_app
from lamplighter.orchestrator import Orchestrator
from lamplighter.policy import Policy
from lamplighter.store import Store
from tests.helpers import FakeActuator, FakeTelemetry, settings

TOKEN, SECRET = "t" * 32, "w" * 32


def make(tmp_path, **overrides):
    cfg = settings(tmp_path, **overrides)
    store, tel = Store(cfg.db_path), FakeTelemetry()
    orch = Orchestrator(cfg, store, tel, FakeActuator(), Policy())
    return TestClient(create_app(cfg, store, tel, orch)), store


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def test_reads_need_a_token_unless_public_read_is_on(tmp_path):
    client, _ = make(tmp_path, api_token=TOKEN)
    assert client.get("/healthz").status_code == 200
    for path in ("/v1/incidents", "/v1/status", "/metrics", "/v1/incidents/inc-20260101-000000-abcdef"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=auth("wrong" * 8)).status_code == 401
    assert client.get("/v1/incidents", headers=auth()).status_code == 200
    public, _ = make(tmp_path / "p", public_read=True)
    assert public.get("/v1/incidents").status_code == 200


def test_with_no_token_configured_everything_protected_is_closed(tmp_path):
    client, store = make(tmp_path)
    assert client.get("/v1/incidents").status_code == 401
    assert client.get("/v1/incidents", headers={"Authorization": "Bearer "}).status_code == 401
    body = {"decision": "approved", "plan_hash": "a" * 64, "actor": "x"}
    assert client.post("/v1/incidents/inc-20260101-000000-abcdef/decision", json=body).status_code == 503
    assert client.post("/v1/alerts", content=b"{}").status_code == 503


def test_decision_requires_token_matching_plan_and_waiting_incident(tmp_path):
    client, store = make(tmp_path, api_token=TOKEN, public_read=True)
    incident = store.open_incident("gateway", {}, time.time())
    incident["data"]["plan_hash"] = "a" * 64
    store.transition(incident, "AWAITING_APPROVAL", "approval_requested", {}, time.time())
    url = f"/v1/incidents/{incident['id']}/decision"
    good = {"decision": "approved", "plan_hash": "a" * 64, "actor": "jay"}
    assert client.post(url, json=good).status_code == 401                       # public read is not public write
    assert client.post(url, json={**good, "plan_hash": "b" * 64}, headers=auth()).status_code == 409
    assert client.post(url, json={**good, "decision": "maybe"}, headers=auth()).status_code == 422
    assert client.post(url, json={**good, "actor": "<script>"}, headers=auth()).status_code == 422
    assert client.post(url, json=good, headers=auth()).status_code == 200
    assert client.post(url, json=good, headers=auth()).status_code == 409        # already decided
    assert store.approval(incident["id"])["actor"] == "jay"


def sign(body: bytes, stamp: int, secret: str = SECRET) -> dict:
    digest = hmac.new(secret.encode(), str(stamp).encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"X-Lamplighter-Timestamp": str(stamp), "X-Lamplighter-Signature": "sha256=" + digest}


def test_webhook_checks_signature_freshness_and_service(tmp_path):
    client, store = make(tmp_path, webhook_secret=SECRET)
    body = json.dumps({"service": "gateway", "source": "pager"}).encode()
    now = int(time.time())
    assert client.post("/v1/alerts", content=body).status_code == 401
    assert client.post("/v1/alerts", content=body, headers=sign(body, now, "x" * 32)).status_code == 401
    assert client.post("/v1/alerts", content=body, headers=sign(body, now - 3600)).status_code == 401   # replayed
    tampered = body.replace(b"gateway", b"payment")
    assert client.post("/v1/alerts", content=tampered, headers=sign(body, now)).status_code == 401
    ok = client.post("/v1/alerts", content=body, headers=sign(body, now))
    assert ok.status_code == 200 and ok.json()["incident"].startswith("inc-")
    again = client.post("/v1/alerts", content=body, headers=sign(body, now))
    assert again.json()["incident"] is None                                      # deduplicated
    unknown = json.dumps({"service": "not-a-service"}).encode()
    assert client.post("/v1/alerts", content=unknown, headers=sign(unknown, now)).json()["incident"] is None
    huge = b"x" * 20000
    assert client.post("/v1/alerts", content=huge, headers=sign(huge, now)).status_code == 413


@pytest.mark.parametrize("bad_id", ["../../etc/passwd", "inc-1", "inc-20260101-000000-ABCDEF", "x" * 500])
def test_incident_ids_are_validated(tmp_path, bad_id):
    client, _ = make(tmp_path, public_read=True)
    assert client.get(f"/v1/incidents/{bad_id}").status_code == 404
    assert client.get(f"/v1/incidents/{bad_id}/postmortem").status_code == 404


def test_security_headers_and_static_allow_list(tmp_path):
    client, _ = make(tmp_path, public_read=True)
    page = client.get("/")
    assert page.status_code == 200 and "script-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY" and page.headers["x-content-type-options"] == "nosniff"
    assert 'src="/static/app.bundle.js"' in page.text and "onclick" not in page.text            # no inline script
    assert client.get("/static/app.bundle.js").status_code == 200
    assert client.get("/static/icon-alarm.svg").headers["content-type"].startswith("image/svg+xml")
    assert client.get("/static/fonts/oxanium-latin-wght-normal.woff2").status_code == 200
    assert client.get("/static/fonts/LICENSES.txt").status_code == 404 and client.get("/static/fonts/..%2Fstyle.css").status_code == 404
    assert "font-src 'self'" in page.headers["content-security-policy"]
    for name in ("../api.py", "index.html", "..%2Fapi.py", "app.bundle.js.map"):
        assert client.get(f"/static/{name}").status_code == 404
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404


def test_incident_view_redacts_secrets(tmp_path):
    client, store = make(tmp_path, public_read=True)
    incident = store.open_incident("gateway", {"note": "password=hunter2 from a@b.co"}, time.time())
    text = client.get(f"/v1/incidents/{incident['id']}").text
    assert "hunter2" not in text and "a@b.co" not in text


def test_demo_fault_routes_exist_only_in_the_demo_and_cannot_be_triggered_cross_site(tmp_path):
    client, _ = make(tmp_path, public_read=True)
    assert client.get("/v1/demo/scenarios").status_code == 404           # a normal deployment has no such routes
    assert client.post("/v1/demo/scenarios/bad_release").status_code == 404

    cfg = settings(tmp_path / "demo", public_read=True)
    store, tel, fired = Store(cfg.db_path), FakeTelemetry(), []
    orch = Orchestrator(cfg, store, tel, FakeActuator(), Policy())
    hooks = {"bad_release": {"label": "Ship a broken release", "what": "x", "expect": "Roll back", "run": lambda: fired.append(1)},
             "reset": {"label": "Reset", "what": "x", "expect": "", "run": lambda: fired.append("reset")}}
    demo = TestClient(create_app(cfg, store, tel, orch, demo=hooks))
    assert [s["name"] for s in demo.get("/v1/demo/scenarios").json()["scenarios"]] == ["bad_release", "reset"]
    assert demo.post("/v1/demo/scenarios/bad_release").status_code == 403    # no custom header: a cross-site form post
    assert demo.post("/v1/demo/scenarios/nope", headers={"X-Lamplighter-Demo": "1"}).status_code == 404
    assert demo.post("/v1/demo/scenarios/bad_release", headers={"X-Lamplighter-Demo": "1"}).status_code == 200
    store.open_incident("gateway", {}, time.time())
    assert demo.post("/v1/demo/scenarios/bad_release", headers={"X-Lamplighter-Demo": "1"}).status_code == 409
    assert demo.post("/v1/demo/scenarios/reset", headers={"X-Lamplighter-Demo": "1"}).status_code == 200
    assert fired == [1, "reset"]
    settled = TestClient(create_app(settings(tmp_path / "s", public_read=True), Store(tmp_path / "s.db"), tel,
                                    orch, demo=hooks))
    tel.change("chg-1", time.time() - 5, "deploy", "checkout", "v1", "v2")       # a human change 5 seconds ago
    assert settled.get("/v1/status").json()["inputs"]["settling_s"] > 0
    assert settled.post("/v1/demo/scenarios/bad_release", headers={"X-Lamplighter-Demo": "1"}).status_code == 409
    assert settled.post("/v1/demo/scenarios/reset", headers={"X-Lamplighter-Demo": "1"}).status_code == 200
    status = demo.get("/v1/status").json()
    assert status["inputs"]["environment"] == "demo shop" and status["inputs"]["policy"]["restart"] == "approval"
