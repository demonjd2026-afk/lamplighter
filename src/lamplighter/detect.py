"""Detection: multi-window error-budget burn on the edge service's SLO.

A request is "bad" when the server failed it (status >= 500) or it exceeded the
latency objective. Client errors (4xx) never count: they are the caller's
mistake, not an outage. An alert needs both a short and a long window over the
burn threshold, so a brief blip cannot page and a recovered incident stops
paging quickly.
"""
from __future__ import annotations

from .config import Timing
from .telemetry import TelemetrySource


def is_bad(request: dict, slo: dict) -> bool:
    if request.get("status", 0) >= 500:
        return True
    limit = slo.get("latency_ms")
    return bool(limit) and request.get("latency_ms", 0) > limit


def window(telemetry: TelemetrySource, service: str, slo: dict, start: float, end: float) -> dict:
    requests = telemetry.requests(service, start, end)
    bad = sum(1 for r in requests if is_bad(r, slo))
    total = len(requests)
    return {"start": start, "end": end, "total": total, "bad": bad, "bad_fraction": bad / total if total else 0.0}


def slo_services(catalog: dict) -> dict[str, dict]:
    out = {}
    for name, meta in catalog.get("services", {}).items():
        slo = meta.get("slo") if isinstance(meta, dict) else None
        if isinstance(slo, dict) and 0 < slo.get("target", 0) < 1:
            out[name] = slo
    return out


def evaluate(telemetry: TelemetrySource, service: str, slo: dict, now: float, timing: Timing) -> dict | None:
    """Return an alert when the service is burning its error budget too fast."""
    budget = 1.0 - slo["target"]
    short = window(telemetry, service, slo, now - timing.short_window_s, now)
    if short["total"] < timing.min_requests:
        return None
    burn_short = short["bad_fraction"] / budget
    if burn_short < timing.burn_threshold:
        return None
    long = window(telemetry, service, slo, now - timing.long_window_s, now)
    burn_long = long["bad_fraction"] / budget
    if burn_long < timing.burn_threshold:
        return None
    return {"service": service, "fired_at": now, "slo": slo, "budget": budget,
            "burn_short": round(burn_short, 2), "burn_long": round(burn_long, 2),
            "short": short, "long": long, "threshold": timing.burn_threshold}


def still_failing(telemetry: TelemetrySource, service: str, slo: dict, now: float, timing: Timing) -> tuple[bool, dict]:
    """Is the service failing right now (freshest window), as opposed to still
    carrying old errors in its longer windows?"""
    recent = window(telemetry, service, slo, now - timing.recent_window_s, now)
    budget = 1.0 - slo["target"]
    failing = recent["total"] > 0 and recent["bad_fraction"] / budget >= timing.burn_threshold
    return failing, recent
