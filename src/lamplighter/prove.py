"""Proof, before and after an action.

Before (preflight) is an experiment, not a correlation:
  control - replay the failing requests against a sandbox in the CURRENT state;
            the failure must reproduce there, or the suspected change is not
            what breaks them
  fix     - replay failing and golden requests against a sandbox with the fix
            applied; failing ones must be served and golden ones must match
After (postflight) watches live traffic: consecutive healthy windows and no
recurrence of the incident's error signature.
"""
from __future__ import annotations

from .actions import Actuator
from .config import Settings, Timing
from .detect import window
from .evidence import Bundle, _signature
from .telemetry import TelemetrySource
from . import replay

REPRODUCE_SHARE = 0.8


def preflight(plan: dict, cases: dict, actuator: Actuator, settings: Settings) -> dict:
    kind = plan["kind"]
    if kind == "restart":
        return {"applicable": False, "passed": True,
                "note": "a restart cannot be rehearsed in a sandbox; it is proven on live traffic afterwards"}
    if kind == "rollback_release":
        fix_spec = ({plan["service"]: plan["to"]}, {})
    elif kind == "revert_config":
        fix_spec = ({}, {plan["key"]: plan["to"]})
    else:
        return {"applicable": True, "passed": False, "note": f"no preflight defined for {kind}"}
    if not cases["failing"]:
        return {"applicable": True, "passed": False,
                "note": "no failing request was captured, so the fix cannot be rehearsed"}
    result: dict = {"applicable": True, "passed": False}
    sandboxes: list[str] = []
    try:
        control = actuator.sandbox_create({}, {})
        sandboxes.append(control["id"])
        control_run = replay.run(control["checkout_url"], {"failing": cases["failing"], "golden": []},
                                 settings.noise_keys, settings.allowed_hosts)
        still_failing = control_run["failing"]["total"] - control_run["failing"]["passed"]
        result["control"] = {"state": "current", "failing_total": control_run["failing"]["total"],
                             "failing_reproduced": still_failing}
        result["reproduced"] = still_failing >= REPRODUCE_SHARE * control_run["failing"]["total"]
        actuator.sandbox_delete(sandboxes.pop())
        if not result["reproduced"]:
            result["note"] = ("the failure does not reproduce in an isolated copy of the current state, "
                              "so the suspected change is not what breaks these requests")
            return result
        fixed = actuator.sandbox_create(*fix_spec)
        sandboxes.append(fixed["id"])
        result["fix"] = replay.run(fixed["checkout_url"], cases, settings.noise_keys, settings.allowed_hosts)
        result["passed"] = result["fix"]["passed"]
        result["note"] = ("the failure reproduces with the change and disappears without it; "
                          "healthy traffic is served unchanged" if result["passed"]
                          else "the proposed fix does not pass replay")
        return result
    finally:
        for sandbox_id in sandboxes:
            try:
                actuator.sandbox_delete(sandbox_id)
            except Exception:  # noqa: BLE001 - cleanup must not mask the result
                pass


def tracked_signature(bundle: Bundle) -> dict | None:
    """The incident's dominant error signature, taken from the deepest service on the failure path that has one."""
    for service in reversed(bundle.facts["root"]["path"]):
        signatures = bundle.facts["signatures"].get(service) or []
        if signatures:
            return {"service": service, "signature": signatures[0]["signature"]}
    return None


def postflight(telemetry: TelemetrySource, service: str, slo: dict, action_ts: float, now: float,
               timing: Timing, tracked: dict | None) -> dict:
    """One non-blocking check. `ready` is False until a full window of post-action traffic exists."""
    earliest = action_ts + timing.settle_s
    start = now - timing.verify_window_s
    if start < earliest:
        return {"ready": False}
    stats = window(telemetry, service, slo, start, now)
    recurrence = 0
    if tracked is not None:
        recurrence = sum(1 for r in telemetry.requests(tracked["service"], start, now)
                         if r.get("status", 0) >= 500 and r.get("error") and _signature(r) == tracked["signature"])
    healthy = (stats["total"] >= timing.verify_min_requests and stats["bad_fraction"] <= timing.verify_max_bad
               and recurrence == 0)
    return {"ready": True, "healthy": healthy, "window": stats, "recurrence": recurrence, "checked_at": now}
