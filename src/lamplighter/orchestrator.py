"""The incident state machine.

    DETECTED -> [AWAITING_APPROVAL] -> PREFLIGHT -> GATE -> ACTING -> VERIFYING -> RESOLVED
         \\-> SELF_RESOLVED (it stopped on its own)        \\-> REVERTING -> ESCALATED
         \\-> ESCALATED (no safe, proven action: a human decides)

Every state is persisted before the next step runs, each step is safe to run
twice, and the worker can be killed at any point and resume where it stopped.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

from . import fixforward, prove, replay, report
from .actions import ActionError, Actuator, TransientError, execute, precondition
from .config import Settings
from .detect import evaluate, slo_services, still_failing
from .evidence import Bundle, collect
from .llm import LLMClient, LLMError, narrate
from .policy import Policy
from .reason import accepted_hypothesis, diagnose
from .store import Store, digest
from .telemetry import TelemetrySource

log = logging.getLogger("lamplighter")


class Orchestrator:
    def __init__(self, settings: Settings, store: Store, telemetry: TelemetrySource, actuator: Actuator,
                 policy: Policy, llm: LLMClient | None = None, clock: Callable[[], float] = time.time) -> None:
        self.settings, self.store, self.telemetry = settings, store, telemetry
        self.actuator, self.policy, self.llm, self.clock = actuator, policy, llm, clock
        self.timing = settings.timing
        self._owner = f"{os.getpid()}-{id(self)}"

    # ---- loop ------------------------------------------------------------
    def run(self, stop: threading.Event, interval_s: float = 1.0) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("tick failed")
            stop.wait(interval_s)

    def tick(self) -> None:
        now = self.clock()
        if not self.store.lease(self._owner, 30.0, now):
            return  # another worker is active
        self._detect(now)
        for incident in self.store.list_incidents(open_only=True):
            self.advance(incident)

    def _detect(self, now: float) -> None:
        for service, slo in slo_services(self.telemetry.catalog()).items():
            alert = evaluate(self.telemetry, service, slo, now, self.timing)
            latch = self.store.kv_get(f"latch:{service}")
            if latch is not None:
                # after an incident closes, the alert must clear once before a new incident may open
                if alert is None and not latch.get("cleared"):
                    self.store.kv_set(f"latch:{service}", {**latch, "cleared": True})
                    continue
                if not latch.get("cleared") or now - latch["closed_at"] < self.timing.cooldown_s:
                    continue
            if alert is not None:
                incident = self.store.open_incident(service, alert, now)
                if incident is not None:
                    log.info("incident %s opened for %s (burn %.1f)", incident["id"], service, alert["burn_short"])

    def open_external(self, service: str, source: str) -> dict | None:
        """Open an incident from an external alert (webhook). The service must have an SLO in the catalogue."""
        slo = slo_services(self.telemetry.catalog()).get(service)
        if slo is None:
            return None
        now = self.clock()
        alert = {"service": service, "fired_at": now, "slo": slo, "budget": 1.0 - slo["target"], "source": source}
        return self.store.open_incident(service, alert, now)

    # ---- stepping --------------------------------------------------------
    def advance(self, incident: dict) -> None:
        state, now = incident["state"], self.clock()
        handler = getattr(self, f"_on_{state.lower()}", None)
        if handler is None:
            return
        attempts = incident["data"].setdefault("attempts", {})
        try:
            handler(incident, now)
            attempts.pop(state, None)
        except Exception as exc:  # noqa: BLE001 - classify, count, and escalate when out of attempts
            attempts[state] = attempts.get(state, 0) + 1
            transient = isinstance(exc, TransientError)
            log.warning("step %s of %s failed (%s): %s", state, incident["id"], exc.__class__.__name__, exc)
            self.store.audit(incident["id"], "step_error", {"state": state, "attempt": attempts[state],
                                                            "error": exc.__class__.__name__, "detail": str(exc)[:300]})
            if attempts[state] >= self.timing.max_step_attempts or not (transient or state == "DETECTED"):
                self._close(incident, "ESCALATED", f"step {state} failed: {exc.__class__.__name__}: {str(exc)[:200]}",
                            self.clock())
            else:
                self.store.save_data(incident, self.clock())

    def _move(self, incident: dict, state: str, event: str, detail: object = None) -> None:
        self.store.transition(incident, state, event, detail, self.clock())

    def _bundle(self, incident: dict) -> Bundle:
        return Bundle.from_dict(incident["data"]["bundle"])

    def _failing_now(self, incident: dict, now: float) -> bool:
        failing, recent = still_failing(self.telemetry, incident["service"], incident["data"]["alert"]["slo"],
                                        now, self.timing)
        incident["data"]["recent"] = recent
        return failing

    def _on_detected(self, incident: dict, now: float) -> None:
        data = incident["data"]
        slo = data["alert"]["slo"]
        bundle = collect(self.telemetry, incident["service"], slo, now, self.timing)
        diagnosis = diagnose(bundle, self.telemetry.catalog(), self.timing)
        data["bundle"], data["diagnosis"] = bundle.to_dict(), diagnosis
        age = now - incident["opened_at"]
        failing = self._failing_now(incident, now)
        hypothesis = accepted_hypothesis(diagnosis)
        if hypothesis is not None and hypothesis["plan"] is not None:
            data["plan"], data["plan_hash"] = hypothesis["plan"], digest(hypothesis["plan"])
            decision = self.policy.decide(hypothesis["plan"], self.store, now)
            data["policy"] = decision
            if decision["verdict"] == "deny":
                self._close(incident, "ESCALATED", "policy: " + "; ".join(decision["reasons"]), now)
            elif decision["verdict"] == "approval":
                self._move(incident, "AWAITING_APPROVAL", "approval_requested",
                           {"plan": data["plan"], "plan_hash": data["plan_hash"]})
                self._notify(incident, "approval needed")
            else:
                self._move(incident, "PREFLIGHT", "diagnosed", {"hypothesis": hypothesis["id"], "plan": data["plan"]})
            return
        if not failing and age >= self.timing.hold_s:
            self._close(incident, "SELF_RESOLVED", "the service recovered on its own before any action", now)
        elif hypothesis is not None and failing and age >= self.timing.hold_s:
            self._close(incident, "ESCALATED", f"{hypothesis['summary']}: no safe automated action exists", now)
        elif failing and age >= 2 * self.timing.hold_s:
            self._close(incident, "ESCALATED", "diagnosis inconclusive: " + diagnosis["reason"], now)
        else:
            self.store.save_data(incident, now)  # keep watching; evidence accumulates

    def _on_awaiting_approval(self, incident: dict, now: float) -> None:
        decision = self.store.approval(incident["id"])
        if decision is not None:
            if decision["decision"] == "approved":
                self._move(incident, "PREFLIGHT", "approved", {"actor": decision["actor"]})
            else:
                self._close(incident, "ESCALATED", f"plan rejected by {decision['actor']}", now)
            return
        requested_at = incident["data"]["timeline"][-1]["ts"]
        if not self._failing_now(incident, now) and now - incident["opened_at"] >= self.timing.hold_s:
            self._close(incident, "SELF_RESOLVED", "the service recovered while waiting for approval", now)
        elif now - requested_at > self.timing.approval_ttl_s:
            self._close(incident, "ESCALATED", "approval request expired", now)

    def _on_preflight(self, incident: dict, now: float) -> None:
        data = incident["data"]
        cases = replay.select_cases(self.telemetry, self._bundle(incident), self.timing)
        self._artefact(incident, "replay_cases.json", json.dumps(cases, indent=1))
        result = prove.preflight(data["plan"], cases, self.actuator, self.settings)
        data["preflight"] = result
        if result["passed"]:
            self._move(incident, "GATE", "preflight_passed", result)
        else:
            self._close(incident, "ESCALATED", "preflight refuted the plan: " + result.get("note", ""), now)

    def _on_gate(self, incident: dict, now: float) -> None:
        data = incident["data"]
        if self.policy.kill_switch_on():
            self._close(incident, "ESCALATED", "kill switch is engaged", now)
            return
        failing = self._failing_now(incident, now)
        if now - incident["opened_at"] < self.timing.hold_s:
            self.store.save_data(incident, now)
            return  # the alert has not persisted long enough to justify acting
        if not failing:
            self._close(incident, "SELF_RESOLVED", "the service recovered on its own before any action", now)
            return
        ok, detail = precondition(self.actuator, data["plan"])
        if not ok:
            self._close(incident, "ESCALATED", "the plan is stale: " + detail, now)
            return
        decision = self.policy.decide(data["plan"], self.store, now)
        if decision["verdict"] == "deny":
            self._close(incident, "ESCALATED", "policy: " + "; ".join(decision["reasons"]), now)
            return
        self._move(incident, "ACTING", "gate_passed", {"recent": data["recent"], "precondition": detail})

    def _on_acting(self, incident: dict, now: float) -> None:
        data = incident["data"]
        key = f"{incident['id']}:{data['plan_hash'][:16]}:do"
        record = self.store.action_begin(key, incident["id"], data["plan"]["kind"], data["plan"], now)
        if record["status"] != "DONE":
            if record["status"] == "FAILED":
                self._close(incident, "ESCALATED", "the action failed", now)
                return
            try:
                result = execute(self.actuator, data["plan"], key)  # idempotent by key: safe to repeat
            except ActionError as exc:
                self.store.action_finish(key, "FAILED", {"error": str(exc)[:300]}, self.clock())
                self._close(incident, "ESCALATED", f"the control plane refused the action: {str(exc)[:200]}", now)
                return
            if not result.get("ok"):
                self.store.action_finish(key, "FAILED", result, self.clock())
                self._close(incident, "ESCALATED", "the action was applied but the service did not come back healthy",
                            now)
                return
            self.store.action_finish(key, "DONE", result, self.clock())
            record = {"result": result}
        change = (record.get("result") or {}).get("change") or {}
        data["action"] = {"key": key, "ts": max(self.clock(), change.get("ts", 0.0)), "result": record.get("result")}
        data["verify"] = {"streak": 0, "checks": [], "last": 0.0}
        self._move(incident, "VERIFYING", "action_done", {"key": key})

    def _on_verifying(self, incident: dict, now: float) -> None:
        data = incident["data"]
        verify, action = data["verify"], data["action"]
        if now - verify["last"] >= self.timing.verify_window_s:
            check = prove.postflight(self.telemetry, incident["service"], data["alert"]["slo"], action["ts"], now,
                                     self.timing, prove.tracked_signature(self._bundle(incident)))
            if check["ready"]:
                verify["last"] = now
                verify["streak"] = verify["streak"] + 1 if check["healthy"] else 0
                verify["checks"] = (verify["checks"] + [check])[-20:]
        if verify["streak"] >= self.timing.verify_checks:
            self._close(incident, "RESOLVED", "service restored and proven on live traffic", now)
        elif now - action["ts"] > self.timing.verify_timeout_s:
            if data["plan"].get("inverse"):
                self._move(incident, "REVERTING", "proof_failed", {"checks": verify["checks"][-3:]})
            else:
                self._close(incident, "ESCALATED", "the action did not restore the service", now)
        else:
            self.store.save_data(incident, now)

    def _on_reverting(self, incident: dict, now: float) -> None:
        data = incident["data"]
        key = f"{incident['id']}:{data['plan_hash'][:16]}:undo"
        record = self.store.action_begin(key, incident["id"], "undo:" + data["plan"]["kind"], data["plan"]["inverse"], now)
        if record["status"] == "PENDING":
            try:
                result = execute(self.actuator, data["plan"]["inverse"], key)
            except ActionError as exc:
                result = {"ok": False, "error": str(exc)[:300]}
            self.store.action_finish(key, "DONE" if result.get("ok") else "FAILED", result, self.clock())
            record = {"status": "DONE" if result.get("ok") else "FAILED"}
        data["revert"] = {"key": key, "status": record["status"]}
        undone = "and was undone" if record["status"] == "DONE" else "and undoing it FAILED: check the service now"
        self._close(incident, "ESCALATED", f"the action did not restore the service {undone}", now)

    # ---- closing ---------------------------------------------------------
    def _close(self, incident: dict, state: str, reason: str, now: float) -> None:
        data = incident["data"]
        onset = (data.get("bundle") or {}).get("onset")
        data["outcome"] = {"state": state, "reason": reason,
                           "time_to_detect_s": round(incident["opened_at"] - onset, 2) if onset else None,
                           "time_to_close_s": round(now - onset, 2) if onset else None}
        self.store.transition(incident, state, "closed", {"reason": reason}, now)
        self.store.kv_set(f"latch:{incident['service']}", {"closed_at": now, "cleared": False})
        log.info("incident %s %s: %s", incident["id"], state, reason)
        # everything below is best effort: the incident is already durably closed
        try:
            self._post_close(incident)
        except Exception:  # noqa: BLE001
            log.exception("post-close work failed for %s", incident["id"])
        self._notify(incident, state.lower())

    def _post_close(self, incident: dict) -> None:
        data = incident["data"]
        if "bundle" not in data:
            return
        bundle = self._bundle(incident)
        plan = data.get("plan")
        if incident["state"] == "RESOLVED" and plan and plan["kind"] == "rollback_release":
            try:
                cases = json.loads(self._artefact_path(incident, "replay_cases.json").read_text(encoding="utf-8"))
                signatures = bundle.facts["signatures"].get(plan["service"]) or [None]
                brief = fixforward.build(self.settings, plan, signatures[0], cases, self.actuator, self.llm)
                if brief is not None:
                    data["fixforward"] = brief
                    if brief["patch"].get("diff"):
                        self._artefact(incident, "fix-forward.patch", brief["patch"]["diff"] + "\n")
            except Exception as exc:  # noqa: BLE001
                data["fixforward_error"] = exc.__class__.__name__
        if self.llm is not None:
            try:
                data["narrative"] = narrate(self.llm, bundle, data["diagnosis"], data["outcome"])
            except LLMError as exc:
                data["narrative_error"] = str(exc)
        self.store.save_data(incident, self.clock())
        intact, entries, _ = self.store.verify_audit()
        trail = self.store.audit_trail(incident["id"])
        text = report.render(incident, self.store.actions_for(incident["id"]),
                             {"intact": intact, "entries": entries, "head": trail[-1]["hash"] if trail else None})
        self._artefact(incident, "postmortem.md", text)

    def _artefact_path(self, incident: dict, name: str) -> Path:
        return self.settings.data_dir / "incidents" / incident["id"] / name

    def _artefact(self, incident: dict, name: str, content: str) -> None:
        path = self._artefact_path(incident, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _notify(self, incident: dict, event: str) -> None:
        if not self.settings.notify_url:
            return
        outcome = incident["data"].get("outcome") or {}
        body = json.dumps({"incident": incident["id"], "service": incident["service"], "state": incident["state"],
                           "event": event, "reason": outcome.get("reason"),
                           "plan_hash": incident["data"].get("plan_hash")}).encode()
        request = urllib.request.Request(self.settings.notify_url, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request, timeout=5).close()
        except Exception as exc:  # noqa: BLE001 - notification failure must not affect the incident
            log.warning("notification failed: %s", exc.__class__.__name__)
