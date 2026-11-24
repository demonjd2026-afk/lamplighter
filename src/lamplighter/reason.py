"""Diagnosis by explicit, checkable hypotheses.

A hypothesis is a claim "component X failed because of cause C". Each one carries
named checks, every check cites evidence ids, and a hypothesis is accepted only
when all of its required checks pass. Exactly one accepted hypothesis is a
diagnosis; none or several means a human decides. There is no free-text
reasoning step whose output can trigger an action.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .config import Timing
from .evidence import Bundle

CAUSES = ("bad_release", "bad_config", "service_degraded", "process_down", "external_dependency",
          "unexplained_errors")
AUTOMATION_ACTOR = "lamplighter"


@dataclass
class Check:
    name: str
    required: bool
    passed: bool
    detail: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class Hypothesis:
    id: str
    component: str
    cause_class: str
    summary: str
    change_id: str | None = None
    checks: list[Check] = field(default_factory=list)
    accepted: bool = False
    score: float = 0.0
    plan: dict | None = None

    def finalise(self) -> "Hypothesis":
        self.accepted = all(c.passed for c in self.checks if c.required)
        self.score = round(sum(c.passed for c in self.checks) / len(self.checks), 2) if self.checks else 0.0
        return self


def _ids(*values: str | None) -> list[str]:
    return [v for v in values if v]


def _later_change(change: dict, changes: list[dict]) -> dict | None:
    for other in changes:
        if (other["ts"] > change["ts"] and other["kind"] == change["kind"]
                and other["service"] == change["service"] and other.get("key") == change.get("key")):
            return other
    return None


def _temporal(change: dict, timing: Timing) -> Check:
    lag = change["lag_s"]
    ok = 0 <= lag <= timing.change_lag_s
    detail = (f"the change landed {lag:.1f}s before the first failure" if lag >= 0
              else f"the change landed {-lag:.1f}s after failures began, so it cannot be the cause")
    return Check("change precedes onset", True, ok, detail, _ids(change.get("evidence")))


def _release_hypothesis(change: dict, bundle: Bundle, timing: Timing) -> Hypothesis:
    facts, service = bundle.facts, change["service"]
    root = facts["root"]
    hyp = Hypothesis("", service, "bad_release",
                     f"release {change['to']} of {service} introduced a defect", change_id=change["id"])
    hyp.checks.append(_temporal(change, timing))
    hyp.checks.append(Check(
        "failures originate in this service", True, root["service"] == service and root["mode"] == "local",
        f"failure path {' -> '.join(root['path'])} ends at {root['service']} ({root['mode']})", root["evidence"]))
    signatures = facts["signatures"].get(service, [])
    top = signatures[0] if signatures else None
    code_ok = bool(top and top["novel"] and not top["upstream"])
    if top:
        where = f" at {top['frame']['file']}:{top['frame']['line']}" if top["frame"] else ""
        detail = (f"dominant error \"{top['signature']}\"{where} is "
                  f"{'new since the change' if top['novel'] else 'also present before the change'}"
                  f"{'' if not top['upstream'] else ' but is blamed on ' + top['upstream']}")
    else:
        detail = "no error raised inside this service"
    hyp.checks.append(Check("new error raised by this service's code", True, code_ok, detail,
                            _ids(top["evidence"] if top else None)))
    later = _later_change(change, facts["changes"])
    hyp.checks.append(Check("this release is still the running one", True, later is None,
                            "no later deploy of this service" if later is None
                            else f"superseded by {later['id']}", _ids(later.get("evidence") if later else None)))
    split = facts["release_split"].get(service)
    if split:
        releases = split["releases"]
        new, old = releases.get(str(change["to"]), {"total": 0, "bad": 0}), releases.get(str(change["from"]))
        all_bad = sum(r["bad"] for r in releases.values())
        concentrated = all_bad > 0 and new["bad"] / all_bad >= 0.9
        old_clean = old is not None and old["total"] > 0 and old["bad"] / old["total"] < 0.02
        hyp.checks.append(Check(
            "errors are confined to the new release", False, concentrated and old_clean,
            f"{new['bad']} of {all_bad} server errors came from {change['to']}; "
            f"{change['from']} had {old['bad']}/{old['total']}" if old else "no traffic recorded on the previous release",
            _ids(split["evidence"])))
    hyp.plan = {"kind": "rollback_release", "service": service, "from": change["to"], "to": change["from"],
                "inverse": {"kind": "deploy", "service": service, "release": change["to"]}}
    return hyp.finalise()


def _config_hypothesis(change: dict, bundle: Bundle, timing: Timing) -> Hypothesis:
    facts, service, key = bundle.facts, change["service"], str(change.get("key"))
    root = facts["root"]
    governed = key.split(".", 1)[0]
    hyp = Hypothesis("", service, "bad_config",
                     f"config {key} of {service} was changed from {change['from']} to {change['to']}",
                     change_id=change["id"])
    hyp.checks.append(_temporal(change, timing))
    origin_ok = root["service"] == service and root["mode"] == "caller_side" and root.get("target") == governed
    hyp.checks.append(Check(
        "failures are on the call this setting governs", True, origin_ok,
        f"failure path {' -> '.join(root['path'])} ends at {root['service']} ({root['mode']}"
        f"{', calls to ' + root['target'] if root.get('target') else ''}); the setting governs calls to {governed}",
        root["evidence"]))
    later = _later_change(change, facts["changes"])
    hyp.checks.append(Check("this value is still in effect", True, later is None,
                            "no later change of this key" if later is None else f"superseded by {later['id']}",
                            _ids(later.get("evidence") if later else None)))
    target = facts["health"].get(governed)
    if target is not None:
        hyp.checks.append(Check(
            "the dependency is healthy from its own side", False, not target["anomalous"],
            f"{governed} reports {target['err_inc']:.1%} errors and p95 {target['p95_inc_ms']:.0f}ms on its own telemetry",
            _ids(target.get("evidence"))))
        value = change.get("to")
        if key.endswith("timeout_ms") and isinstance(value, (int, float)) and not isinstance(value, bool):
            hyp.checks.append(Check(
                "the new value explains the failures", False, value < target["p95_base_ms"],
                f"timeout {value}ms versus the dependency's normal p95 of {target['p95_base_ms']:.0f}ms",
                _ids(target.get("evidence"), change.get("evidence"))))
    hyp.plan = {"kind": "revert_config", "service": service, "key": key, "from": change["to"], "to": change["from"],
                "inverse": {"kind": "set_config", "service": service, "key": key, "value": change["to"]}}
    return hyp.finalise()


def _root_hypothesis(bundle: Bundle, catalog: dict, timing: Timing) -> Hypothesis | None:
    facts = bundle.facts
    root = facts["root"]
    service = root["service"]
    if root["mode"] not in ("local", "silent") or service == bundle.service and not facts["attribution"].get(
            service, {}).get("failures"):
        return None
    meta = catalog.get("services", {}).get(service, {})
    health = facts["health"][service]
    signatures = facts["signatures"].get(service, [])
    kinds: dict[str, int] = {}
    for name, edge in facts["edges"].items():
        if name.endswith(f"->{service}"):
            for kind, count in edge["kinds"].items():
                kinds[kind] = kinds.get(kind, 0) + count
    inbound_failures = sum(kinds.values())
    unreachable = kinds.get("unreachable", 0) / inbound_failures if inbound_failures else 0.0
    local_exceptions = [s for s in signatures if not s["upstream"]]

    if meta.get("external"):
        cause, summary = "external_dependency", f"external dependency {service} is failing"
    elif "silent" in health["reasons"] and unreachable >= 0.6:
        cause, summary = "process_down", f"{service} is not accepting connections"
    elif local_exceptions:
        cause, summary = "unexplained_errors", f"{service} is raising errors with no change to explain them"
    else:
        cause, summary = "service_degraded", f"{service} is unresponsive or slow with no change to explain it"
    hyp = Hypothesis("", service, cause, summary)
    hyp.checks.append(Check("failure path ends at this service", True, True,
                            f"failure path {' -> '.join(root['path'])} ends at {service} ({root['mode']})",
                            root["evidence"]))
    hyp.checks.append(Check(
        "unhealthy by its own telemetry or unreachable", True, health["anomalous"],
        f"{service}: {', '.join(health['reasons']) or 'no anomaly'}; callers saw {kinds or 'no call failures'}",
        _ids(health.get("evidence"))))
    if cause != "external_dependency":
        suspects = [c for c in facts["changes"] if c["service"] == service and c["kind"] in ("deploy", "config")
                    and c.get("actor") != AUTOMATION_ACTOR and 0 <= c["lag_s"] <= timing.change_lag_s]
        hyp.checks.append(Check(
            "no recent change to this service", True, not suspects,
            "no deploy or config change to this service before onset" if not suspects
            else "recent change(s): " + ", ".join(c["id"] for c in suspects),
            _ids(*(c.get("evidence") for c in suspects))))
    if cause in ("service_degraded", "process_down") and meta.get("managed") and meta.get("restart_safe"):
        hyp.plan = {"kind": "restart", "service": service, "inverse": None}
    return hyp.finalise()


def diagnose(bundle: Bundle, catalog: dict, timing: Timing) -> dict:
    hypotheses: list[Hypothesis] = []
    for change in bundle.facts["changes"]:
        if change.get("actor") == AUTOMATION_ACTOR:
            continue  # our own remediation is never a suspect for the incident it answered
        if change["kind"] == "deploy":
            hypotheses.append(_release_hypothesis(change, bundle, timing))
        elif change["kind"] == "config":
            hypotheses.append(_config_hypothesis(change, bundle, timing))
    root_based = _root_hypothesis(bundle, catalog, timing)
    if root_based is not None:
        hypotheses.append(root_based)
    hypotheses.sort(key=lambda h: (not h.accepted, -h.score))
    for index, hyp in enumerate(hypotheses, start=1):
        hyp.id = f"H{index}"
    accepted = [h for h in hypotheses if h.accepted]
    if len(accepted) == 1:
        verdict, reason = "accepted", f"{accepted[0].id} is the only hypothesis whose required checks all pass"
    elif accepted:
        verdict = "ambiguous"
        reason = "more than one hypothesis passes its required checks: " + ", ".join(h.id for h in accepted)
    else:
        verdict, reason = "none", "no hypothesis passes all of its required checks"
    return {"verdict": verdict, "reason": reason, "accepted": accepted[0].id if len(accepted) == 1 else None,
            "hypotheses": [asdict(h) for h in hypotheses], "root": bundle.facts["root"]}


def accepted_hypothesis(diagnosis: dict) -> dict | None:
    if diagnosis.get("verdict") != "accepted":
        return None
    return next(h for h in diagnosis["hypotheses"] if h["id"] == diagnosis["accepted"])
