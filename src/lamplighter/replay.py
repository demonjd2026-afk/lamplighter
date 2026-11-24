"""Recorded-traffic replay: the proof that a fix would work before it ships.

Two sets of cases are cut from the edge's captured traffic:
  failing - requests that failed during the incident; a fix must serve them
  golden  - requests from the healthy baseline, with the responses customers
            received; a fix must reproduce those responses exactly, apart from
            fields declared as noise (ids, timestamps)
Replays run only against a sandbox stack, never against production: a checkout
is not idempotent, and replaying it at live systems would place real orders.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import Timing, validate_url
from .evidence import Bundle
from .telemetry import TelemetrySource

MAX_FAILING, MAX_GOLDEN = 25, 50
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})


def _replayable(record: dict) -> bool:
    route = record.get("route")
    return ("req" in record and record.get("method", "POST") in METHODS and isinstance(route, str)
            and route.startswith("/") and not route.startswith("//") and len(route) <= 512
            and all(32 < ord(ch) < 127 for ch in route))


def _key(request_body: Any) -> str:
    return json.dumps(request_body, sort_keys=True, separators=(",", ":"))


def strip_noise(value: Any, noise: frozenset[str]) -> Any:
    if isinstance(value, dict):
        return {k: strip_noise(v, noise) for k, v in value.items() if k not in noise}
    if isinstance(value, list):
        return [strip_noise(v, noise) for v in value]
    return value


def select_cases(telemetry: TelemetrySource, bundle: Bundle, timing: Timing) -> dict:
    """Unique captured requests: failing ones from the incident, golden ones from the baseline."""
    incident = telemetry.requests(bundle.service, bundle.onset, bundle.now)
    baseline = telemetry.requests(bundle.service, bundle.onset - timing.baseline_s, bundle.onset - timing.bucket_s)
    failing: dict[str, dict] = {}
    for record in incident:
        if record.get("status", 0) >= 500 and _replayable(record) and len(failing) < MAX_FAILING:
            failing.setdefault(_key(record["req"]), {"method": record.get("method", "POST"),
                                                     "route": record.get("route", "/"), "req": record["req"]})
    golden: dict[str, dict] = {}
    for record in baseline:
        if record.get("status", 0) < 500 and _replayable(record) and len(golden) < MAX_GOLDEN:
            golden.setdefault(_key(record["req"]), {"method": record.get("method", "POST"),
                                                    "route": record.get("route", "/"), "req": record["req"],
                                                    "status": record["status"], "resp": record.get("resp")})
    return {"failing": list(failing.values()), "golden": list(golden.values())}


def _send(base_url: str, case: dict, timeout_s: float) -> tuple[int, Any]:
    if case.get("method") not in METHODS:
        return 0, None
    route = case["route"] if isinstance(case.get("route"), str) and case["route"].startswith("/") else "/"
    request = urllib.request.Request(base_url + route, data=json.dumps(case["req"]).encode(),
                                     method=case["method"], headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"null")
        except json.JSONDecodeError:
            return exc.code, None
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return 0, None


def run(base_url: str, cases: dict, noise: frozenset[str], allowed_hosts: frozenset[str],
        timeout_s: float = 5.0) -> dict:
    """Replay the cases against a sandbox and report, case by case, what held."""
    base_url = validate_url(base_url, allowed_hosts, "replay target")
    failing_passed, golden_passed, mismatches = 0, 0, []
    for case in cases["failing"]:
        status, _ = _send(base_url, case, timeout_s)
        if 0 < status < 500:
            failing_passed += 1
        elif len(mismatches) < 5:
            mismatches.append({"set": "failing", "req": case["req"], "got_status": status})
    for case in cases["golden"]:
        status, body = _send(base_url, case, timeout_s)
        if status == case["status"] and strip_noise(body, noise) == strip_noise(case["resp"], noise):
            golden_passed += 1
        elif len(mismatches) < 5:
            mismatches.append({"set": "golden", "req": case["req"], "want_status": case["status"],
                               "got_status": status})
    total_failing, total_golden = len(cases["failing"]), len(cases["golden"])
    passed = failing_passed == total_failing and golden_passed == total_golden and total_failing + total_golden > 0
    return {"passed": passed, "failing": {"total": total_failing, "passed": failing_passed},
            "golden": {"total": total_golden, "passed": golden_passed}, "mismatches": mismatches}
