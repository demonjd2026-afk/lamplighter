"""Telemetry is written by the systems being watched, so it is treated as hostile input."""
import json

from lamplighter import replay
from lamplighter.evidence import Bundle
from lamplighter.report import render
from lamplighter.telemetry import JsonlTelemetry
from tests.helpers import T0, TIMING


def write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("".join(lines))


def record(**fields):
    base = {"ts": T0, "kind": "request", "service": "gateway", "status": 200, "latency_ms": 5, "error": None}
    return json.dumps({**base, **fields}) + "\n"


def test_malformed_records_are_skipped_and_counted(tmp_path):
    log = tmp_path / "logs" / "gateway.jsonl"
    write(log, [record(), "not json\n", record(status="500"), record(latency_ms="fast"), record(ts="yesterday"),
                record(service=None), record(error="boom"), record(kind="mystery"), "[1,2,3]\n", record(ts=T0 + 1)])
    tel = JsonlTelemetry(tmp_path)
    assert [r["ts"] for r in tel.requests("gateway", T0 - 1, T0 + 5)] == [T0, T0 + 1]
    assert tel.skipped_lines() == 8


def test_partial_lines_wait_and_truncation_restarts(tmp_path):
    log = tmp_path / "logs" / "gateway.jsonl"
    full = record()
    write(log, [full, full[:20]])                         # the writer is mid-line
    tel = JsonlTelemetry(tmp_path)
    assert len(tel.requests("gateway", T0 - 1, T0 + 5)) == 1
    write(log, [full[20:]])                               # the rest of the line arrives
    assert len(tel.requests("gateway", T0 - 1, T0 + 5)) == 2 and tel.skipped_lines() == 0
    log.write_text(record(ts=T0 + 2))                     # rotated: smaller file
    assert T0 + 2 in [r["ts"] for r in tel.requests("gateway", T0 - 1, T0 + 5)]


def test_missing_files_mean_no_data_not_an_error(tmp_path):
    tel = JsonlTelemetry(tmp_path)
    assert tel.requests("gateway", 0, 1) == [] and tel.changes(0, 1) == [] and tel.catalog() == {"services": {}}
    (tmp_path / "catalog.json").write_text("{not json")
    assert tel.catalog() == {"services": {}}


def test_only_ordinary_captured_requests_are_replayed(tmp_path):
    log = tmp_path / "logs" / "gateway.jsonl"
    cart = {"items": [{"sku": "SKU-1", "qty": 1}]}
    write(log, [record(ts=T0 + 50, status=500, method="POST", route="/checkout", req=cart, error={"type": "X"}),
                record(ts=T0 + 51, status=500, method="GET\r\nX-Evil: 1", route="/checkout", req=cart),
                record(ts=T0 + 52, status=500, method="POST", route="//evil.test/x", req=cart),
                record(ts=T0 + 53, status=500, method="POST", route="http://evil.test/", req=cart),
                record(ts=T0 + 54, status=500, method="POST", route="/a b\r\nHost: evil", req=cart),
                record(ts=T0 + 55, status=500, method="TRACE", route="/checkout", req=cart)])
    bundle = Bundle(service="gateway", now=T0 + 60, onset=T0 + 49)
    cases = replay.select_cases(JsonlTelemetry(tmp_path), bundle, TIMING)
    assert cases["failing"] == [{"method": "POST", "route": "/checkout", "req": cart}]


def test_replay_refuses_targets_outside_the_allow_list():
    try:
        replay.run("http://169.254.169.254", {"failing": [], "golden": []}, frozenset(), frozenset({"127.0.0.1"}))
    except ValueError as exc:
        assert "not in the allowed list" in str(exc)
    else:
        raise AssertionError("expected a refusal")


def test_report_neutralises_markup_from_telemetry():
    incident = {"id": "inc-20260101-000000-abcdef", "service": "gateway", "state": "ESCALATED", "opened_at": T0,
                "closed_at": T0 + 9, "data": {
                    "outcome": {"reason": "<script>alert(1)</script> | pipe `tick`"}, "timeline": [],
                    "bundle": {"onset": T0, "items": [{"id": "E1", "kind": "error_signature", "service": "x",
                                                         "summary": "<img src=x onerror=alert(1)> token=abcdef123456",
                                                         "data": {}, "source": "q"}]},
                    "diagnosis": {"reason": "none", "hypotheses": []}}}
    text = render(incident, [], {"intact": True, "entries": 1, "head": "h"})
    assert "<script>" not in text and "<img" not in text and "abcdef123456" not in text
    assert "&lt;script&gt;" in text
