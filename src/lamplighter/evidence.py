"""Evidence collection. Deterministic: every number here is computed from
telemetry by code, never produced by a model. Each item gets an id (E1, E2, ...)
and a `source` locator a human can use to check it."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import Timing
from .detect import is_bad
from .telemetry import TelemetrySource

GAP_BUCKETS = 3
DOMINANT_SHARE = 0.6
_HEX = re.compile(r"\b[0-9a-f]{12,}\b")
_NUM = re.compile(r"\d+")


@dataclass
class Evidence:
    id: str
    kind: str
    service: str | None
    summary: str
    data: dict
    source: str


@dataclass
class Bundle:
    service: str
    now: float
    onset: float
    items: list[Evidence] = field(default_factory=list)
    facts: dict = field(default_factory=dict)

    def add(self, kind: str, service: str | None, summary: str, data: dict, source: str) -> str:
        evidence_id = f"E{len(self.items) + 1}"
        self.items.append(Evidence(evidence_id, kind, service, summary, data, source))
        return evidence_id

    def ids(self) -> set[str]:
        return {item.id for item in self.items}

    def to_dict(self) -> dict:
        return {"service": self.service, "now": self.now, "onset": self.onset,
                "items": [asdict(i) for i in self.items], "facts": self.facts}

    @staticmethod
    def from_dict(raw: dict) -> "Bundle":
        return Bundle(raw["service"], raw["now"], raw["onset"], [Evidence(**i) for i in raw["items"]], raw["facts"])


def normalise(message: str) -> str:
    """Collapse the variable parts of an error message into a stable signature."""
    return _NUM.sub("<n>", _HEX.sub("<id>", message))[:200]


def p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]


def closure(catalog: dict, service: str) -> list[str]:
    """The service and everything it depends on, breadth first."""
    services = catalog.get("services", {})
    seen, queue = [service], [service]
    while queue:
        for dep in services.get(queue.pop(0), {}).get("deps", []):
            if dep in services and dep not in seen:
                seen.append(dep)
                queue.append(dep)
    return seen


def find_onset(requests: list[dict], slo: dict, start: float, now: float, timing: Timing) -> float | None:
    """Start of the most recent run of bad buckets, refined to the first bad request."""
    if not requests:
        return None
    count = int((now - start) / timing.bucket_s) + 1
    total, bad, first_bad = [0] * count, [0] * count, [None] * count
    for request in requests:
        index = min(count - 1, max(0, int((request["ts"] - start) / timing.bucket_s)))
        total[index] += 1
        if is_bad(request, slo):
            bad[index] += 1
            if first_bad[index] is None or request["ts"] < first_bad[index]:
                first_bad[index] = request["ts"]
    threshold = min(0.5, 5 * (1.0 - slo["target"]))

    def bad_bucket(i: int) -> bool:
        return total[i] > 0 and bad[i] / total[i] >= threshold

    last = next((i for i in range(count - 1, -1, -1) if bad_bucket(i)), None)
    if last is None:
        return None
    first, quiet = last, 0
    for i in range(last - 1, -1, -1):
        if total[i] == 0:
            continue
        if bad_bucket(i):
            first, quiet = i, 0
        else:
            quiet += 1
            if quiet > GAP_BUCKETS:
                break
    return first_bad[first]


def _health(base: list[dict], inc: list[dict], inbound_attempts: int, inbound_fail: float) -> dict:
    err_base = sum(1 for r in base if r["status"] >= 500) / len(base) if base else 0.0
    err_inc = sum(1 for r in inc if r["status"] >= 500) / len(inc) if inc else 0.0
    p95_base, p95_inc = p95([r["latency_ms"] for r in base]), p95([r["latency_ms"] for r in inc])
    reasons = []
    if inc and err_inc >= 0.05 and err_inc >= 3 * err_base + 0.02:
        reasons.append("errors")
    if len(base) >= 10 and len(inc) >= 3 and p95_inc >= max(3 * p95_base, p95_base + 200.0):
        reasons.append("latency")
    # callers kept trying and mostly failed, yet this service served (almost) none of it
    if len(base) >= 10 and inbound_attempts >= 5 and inbound_fail >= 0.5 and len(inc) < 0.2 * inbound_attempts:
        reasons.append("silent")
    return {"requests_base": len(base), "requests_inc": len(inc), "err_base": round(err_base, 4),
            "err_inc": round(err_inc, 4), "p95_base_ms": round(p95_base, 1), "p95_inc_ms": round(p95_inc, 1),
            "anomalous": bool(reasons), "reasons": reasons}


def collect(telemetry: TelemetrySource, service: str, slo: dict, now: float, timing: Timing) -> Bundle:
    catalog = telemetry.catalog()
    services = catalog.get("services", {})
    lookback_start = now - timing.lookback_s
    edge_requests = telemetry.requests(service, lookback_start, now)
    onset = find_onset(edge_requests, slo, lookback_start, now, timing)
    if onset is None:
        onset = now - timing.short_window_s
    base_start, base_end = onset - timing.baseline_s, onset - timing.bucket_s
    bundle = Bundle(service=service, now=now, onset=onset)
    facts: dict[str, Any] = {"health": {}, "edges": {}, "attribution": {}, "signatures": {}, "changes": [],
                             "release_split": {}}

    inc_edge = [r for r in edge_requests if r["ts"] >= onset]
    bad_edge = sum(1 for r in inc_edge if is_bad(r, slo))
    facts["slo"] = bundle.add(
        "slo_burn", service,
        f"{service}: {bad_edge} of {len(inc_edge)} requests failed the SLO since onset "
        f"({bad_edge / len(inc_edge):.1%})" if inc_edge else f"{service}: no requests since onset",
        {"onset": onset, "requests": len(inc_edge), "bad": bad_edge, "slo": slo},
        f"requests service={service} ts>={onset:.3f}")

    members = closure(catalog, service)
    # call edges first: a callee's silence is judged by what its callers saw
    inbound: dict[str, list[tuple[int, float]]] = {}
    for caller in members:
        calls_base = telemetry.calls(caller, base_start, base_end)
        calls_inc = telemetry.calls(caller, onset, now)
        for target in services.get(caller, {}).get("deps", []):
            base = [c for c in calls_base if c.get("target") == target]
            inc = [c for c in calls_inc if c.get("target") == target]
            fail_base = sum(1 for c in base if c.get("error")) / len(base) if base else 0.0
            fail_inc = sum(1 for c in inc if c.get("error")) / len(inc) if inc else 0.0
            kinds = dict(Counter(c["error"] for c in inc if c.get("error")))
            edge = {"calls_base": len(base), "calls_inc": len(inc), "fail_base": round(fail_base, 4),
                    "fail_inc": round(fail_inc, 4), "kinds": kinds}
            edge["evidence"] = bundle.add(
                "edge_health", caller,
                f"{caller} -> {target}: {fail_inc:.1%} of {len(inc)} calls failed since onset "
                f"(baseline {fail_base:.1%}); kinds {kinds or 'none'}",
                edge.copy(), f"calls service={caller} target={target} ts>={onset:.3f}")
            facts["edges"][f"{caller}->{target}"] = edge
            inbound.setdefault(target, []).append((len(inc), fail_inc))

    for member in members:
        attempts = sum(n for n, _ in inbound.get(member, []))
        failed = sum(n * f for n, f in inbound.get(member, []))
        base = telemetry.requests(member, base_start, base_end)
        inc = inc_edge if member == service else telemetry.requests(member, onset, now)
        health = _health(base, inc, attempts, failed / attempts if attempts else 0.0)
        state = "anomalous (" + ", ".join(health["reasons"]) + ")" if health["anomalous"] else "healthy"
        health["evidence"] = bundle.add(
            "service_health", member,
            f"{member} own telemetry: {state}; errors {health['err_inc']:.1%} (baseline {health['err_base']:.1%}), "
            f"p95 {health['p95_inc_ms']:.0f}ms (baseline {health['p95_base_ms']:.0f}ms), "
            f"{health['requests_inc']} requests since onset",
            health.copy(), f"requests service={member} ts>={onset:.3f}")
        facts["health"][member] = health

        failing = [r for r in inc if r["status"] >= 500]
        origins = Counter((r.get("error") or {}).get("upstream") or "local" for r in failing)
        attribution = {"failures": len(failing),
                       "shares": {k: round(v / len(failing), 4) for k, v in origins.items()} if failing else {}}
        if failing:
            attribution["evidence"] = bundle.add(
                "failure_attribution", member,
                f"{member}: {len(failing)} failed requests since onset, by origin {attribution['shares']}",
                attribution.copy(), f"requests service={member} status>=500 ts>={onset:.3f}")
        facts["attribution"][member] = attribution

        base_sigs = Counter(_signature(r) for r in base if r["status"] >= 500 and r.get("error"))
        grouped: dict[str, list[dict]] = {}
        for request in failing:
            if request.get("error"):
                grouped.setdefault(_signature(request), []).append(request)
        signatures = []
        for sig, group in sorted(grouped.items(), key=lambda kv: -len(kv[1]))[:5]:
            sample = group[0]["error"]
            frames = [f for f in sample.get("stack") or [] if f.get("in_release")]
            entry = {"signature": sig, "type": sample.get("type"), "count_inc": len(group),
                     "count_base": base_sigs.get(sig, 0), "novel": base_sigs.get(sig, 0) == 0,
                     "upstream": sample.get("upstream"), "frame": frames[-1] if frames else None,
                     "first_seen": min(r["ts"] for r in group),
                     "sample_traces": [r.get("trace_id") for r in group[:3]]}
            where = f" at {entry['frame']['file']}:{entry['frame']['line']}" if entry["frame"] else ""
            entry["evidence"] = bundle.add(
                "error_signature", member,
                f"{member}: \"{sig}\"{where} x{len(group)} since onset "
                f"({'not seen' if entry['novel'] else str(entry['count_base']) + ' times'} in baseline)",
                entry.copy(), f"requests service={member} error.signature={sig!r}")
            signatures.append(entry)
        facts["signatures"][member] = signatures

        if services.get(member, {}).get("has_releases"):
            split: dict[str, dict] = {}
            for request in base + inc:
                release = str(request.get("release"))
                stats = split.setdefault(release, {"total": 0, "bad": 0})
                stats["total"] += 1
                stats["bad"] += request["status"] >= 500
            if split:
                facts["release_split"][member] = {
                    "releases": split,
                    "evidence": bundle.add(
                        "release_split", member,
                        f"{member} server errors by release: " + ", ".join(
                            f"{rel} {s['bad']}/{s['total']}" for rel, s in sorted(split.items())),
                        {"releases": split}, f"requests service={member} group_by=release ts>={base_start:.3f}")}

    for change in telemetry.changes(onset - timing.change_lag_s, now):
        entry = {**change, "lag_s": round(onset - change["ts"], 3)}
        when = f"{entry['lag_s']:.1f}s before onset" if entry["lag_s"] >= 0 else f"{-entry['lag_s']:.1f}s after onset"
        what = {"deploy": f"deploy {change.get('from')} -> {change.get('to')}",
                "config": f"config {change.get('key')}: {change.get('from')} -> {change.get('to')}",
                "restart": "restart"}.get(change.get("kind"), str(change.get("kind")))
        entry["evidence"] = bundle.add("change", change.get("service"),
                                       f"{change.get('service')}: {what} by {change.get('actor')}, {when}",
                                       dict(entry), f"changes id={change.get('id')}")
        facts["changes"].append(entry)

    deps = {name: [d for d in services.get(name, {}).get("deps", []) if d in facts["health"]] for name in members}
    facts["root"] = _localise(service, facts, deps)
    bundle.facts = facts
    return bundle


def _signature(request: dict) -> str:
    error = request["error"]
    return f"{error.get('type')}: {normalise(str(error.get('message', '')))}"


def _localise(service: str, facts: dict, deps: dict[str, list[str]]) -> dict:
    """Walk the failure from the edge towards its origin.

    At each hop: if most failures were raised locally, stop here. If most are
    blamed on one dependency, follow it only when that dependency is unhealthy by
    its own telemetry or unreachable; if the dependency looks healthy from its own
    side, the fault is on the caller's side of the call (configuration, network).
    """
    path, current, used = [service], service, []
    for _ in range(16):
        health = facts["health"].get(current, {})
        attribution = facts["attribution"].get(current, {"failures": 0, "shares": {}})
        used.append(health.get("evidence"))
        if attribution["failures"] == 0:
            # no failed requests here (slow rather than failing): follow a single unhealthy dependency
            unhealthy = [d for d in deps.get(current, []) if facts["health"][d]["anomalous"] and d not in path]
            if len(unhealthy) == 1 and "silent" not in health.get("reasons", []):
                used.append(facts["edges"].get(f"{current}->{unhealthy[0]}", {}).get("evidence"))
                path.append(unhealthy[0])
                current = unhealthy[0]
                continue
            mode = "silent" if "silent" in health.get("reasons", []) else "local"
            return {"service": current, "mode": mode, "path": path, "evidence": [e for e in used if e]}
        used.append(attribution.get("evidence"))
        origin, share = max(attribution["shares"].items(), key=lambda kv: kv[1])
        if share < DOMINANT_SHARE:
            return {"service": current, "mode": "mixed", "path": path, "evidence": [e for e in used if e]}
        if origin == "local":
            return {"service": current, "mode": "local", "path": path, "evidence": [e for e in used if e]}
        edge = facts["edges"].get(f"{current}->{origin}", {})
        used.append(edge.get("evidence"))
        target_health = facts["health"].get(origin)
        if target_health is None or origin in path:
            return {"service": current, "mode": "mixed", "path": path, "evidence": [e for e in used if e]}
        if target_health["anomalous"]:
            path.append(origin)
            current = origin
            continue
        used.append(target_health.get("evidence"))
        return {"service": current, "mode": "caller_side", "target": origin, "path": path,
                "evidence": [e for e in used if e]}
    return {"service": current, "mode": "mixed", "path": path, "evidence": [e for e in used if e]}
