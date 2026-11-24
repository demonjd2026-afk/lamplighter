"""State machine behaviour with a fake clock, synthetic telemetry and an in-memory control plane."""
import pytest

from lamplighter import prove
from lamplighter.orchestrator import Orchestrator
from lamplighter.policy import Policy, Rule
from lamplighter.store import Store
from tests.helpers import (T0, TIMING, Clock, FakeActuator, FakeTelemetry, bad_release, healthy, payments_timeouts,
                           settings)

AUTO = Policy(mode="auto", max_actions_per_hour=10, rules={k: Rule("auto") for k in
                                                           ("rollback_release", "revert_config", "restart")})
PASS = {"applicable": True, "passed": True, "reproduced": True, "note": "stub",
        "control": {"failing_total": 5, "failing_reproduced": 5},
        "fix": {"failing": {"total": 5, "passed": 5}, "golden": {"total": 5, "passed": 5}, "passed": True}}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(prove, "preflight", lambda plan, cases, actuator, settings: dict(PASS))
    tel = FakeTelemetry()
    healthy(tel, T0, T0 + 60)
    tel.change("chg-1", T0 + 59.8, "deploy", "checkout", "v1", "v2")
    bad_release(tel, T0 + 60, T0 + 200)          # keeps failing unless a test says otherwise
    clock = Clock(T0 + 64)
    store, actuator = Store(tmp_path / "db.sqlite"), FakeActuator(release="v2")

    def make(policy=AUTO):
        return Orchestrator(settings(tmp_path), store, tel, actuator, policy, clock=clock)

    return tel, clock, store, actuator, make


def drive(orch, clock, until, step=1.0, limit=120):
    for _ in range(limit):
        orch.tick()
        incident = orch.store.list_incidents()[0] if orch.store.list_incidents() else None
        if incident and incident["state"] in until:
            return incident
        clock.now += step
    raise AssertionError(f"never reached {until}; at {incident and incident['state']}")


def recover(tel, clock):
    """From now on the service is healthy again (what a successful rollback looks like)."""
    for records in tel._requests.values():
        records[:] = [r for r in records if r["ts"] < clock.now]
    for records in tel._calls.values():
        records[:] = [c for c in records if c["ts"] < clock.now]
    healthy(tel, clock.now, clock.now + 120)


def test_auto_remediation_runs_once_and_is_proven(world):
    tel, clock, store, actuator, make = world
    orch = make()
    drive(orch, clock, {"VERIFYING"})
    assert actuator.applied == [("deploy", "checkout", "v1")]
    recover(tel, clock)
    incident = drive(orch, clock, {"RESOLVED", "ESCALATED"})
    assert incident["state"] == "RESOLVED"
    assert [c["healthy"] for c in incident["data"]["verify"]["checks"]][-TIMING.verify_checks:] == [True] * TIMING.verify_checks
    assert actuator.applied == [("deploy", "checkout", "v1")]            # exactly one action
    assert (tmp := orch.settings.data_dir / "incidents" / incident["id"] / "postmortem.md").is_file()
    assert "ACCEPTED" in tmp.read_text() and store.verify_audit()[0]


def test_no_action_before_the_hold_period(world):
    tel, clock, store, actuator, make = world
    orch = make()
    orch.tick(); orch.tick(); orch.tick()                                # no time passes
    assert actuator.applied == [] and store.list_incidents()[0]["state"] == "GATE"


def test_self_resolved_incident_is_left_alone(world):
    tel, clock, store, actuator, make = world
    orch = make()
    orch.tick()
    recover(tel, clock)                                                  # it stops failing before the hold expires
    incident = drive(orch, clock, {"SELF_RESOLVED", "RESOLVED", "ESCALATED", "VERIFYING"})
    assert incident["state"] == "SELF_RESOLVED" and actuator.applied == []


def test_lost_response_does_not_cause_a_second_action(world):
    tel, clock, store, actuator, make = world
    actuator.lose_next_response = True                                   # applied, but the reply never arrives
    orch = make()
    drive(orch, clock, {"VERIFYING"})
    assert actuator.applied == [("deploy", "checkout", "v1")]
    assert [a["status"] for a in store.actions_for(store.list_incidents()[0]["id"])] == ["DONE"]


def test_worker_restart_resumes_without_repeating_the_action(world):
    tel, clock, store, actuator, make = world
    drive(make(), clock, {"VERIFYING"})
    clock.now += 31                                                      # the old worker's lease runs out
    recover(tel, clock)
    incident = drive(make(), clock, {"RESOLVED", "ESCALATED"})           # a brand new worker picks it up
    assert incident["state"] == "RESOLVED" and actuator.applied == [("deploy", "checkout", "v1")]


def test_failed_proof_undoes_the_action_and_escalates(world):
    tel, clock, store, actuator, make = world
    incident = drive(make(), clock, {"ESCALATED", "RESOLVED"}, limit=200)  # the rollback does not help
    assert incident["state"] == "ESCALATED" and "undone" in incident["data"]["outcome"]["reason"]
    assert actuator.applied == [("deploy", "checkout", "v1"), ("deploy", "checkout", "v2")]
    assert actuator.releases["checkout"] == "v2"


def test_stale_plan_is_not_executed(world):
    tel, clock, store, actuator, make = world
    orch = make()
    orch.tick(); orch.tick()
    actuator.releases["checkout"] = "v1"                                 # someone else already rolled back
    incident = drive(orch, clock, {"ESCALATED", "VERIFYING", "RESOLVED", "SELF_RESOLVED"})
    assert incident["state"] == "ESCALATED" and "stale" in incident["data"]["outcome"]["reason"]
    assert actuator.applied == []


def test_failed_preflight_blocks_the_action(world, monkeypatch):
    tel, clock, store, actuator, make = world
    monkeypatch.setattr(prove, "preflight", lambda *a: {"applicable": True, "passed": False, "reproduced": False,
                                                        "note": "does not reproduce"})
    incident = drive(make(), clock, {"ESCALATED", "VERIFYING"})
    assert incident["state"] == "ESCALATED" and "preflight" in incident["data"]["outcome"]["reason"]
    assert actuator.applied == []


def test_observe_mode_diagnoses_but_never_acts(world):
    tel, clock, store, actuator, make = world
    incident = drive(make(Policy(mode="observe")), clock, {"ESCALATED", "VERIFYING"})
    assert incident["state"] == "ESCALATED" and actuator.applied == []
    assert incident["data"]["diagnosis"]["verdict"] == "accepted"


def test_kill_switch_stops_a_pre_approved_action(world, tmp_path):
    tel, clock, store, actuator, make = world
    kill = tmp_path / "stop"
    orch = make(Policy(mode="auto", rules=AUTO.rules, kill_switch_file=kill))
    orch.tick(); orch.tick()
    kill.write_text("stop")                                              # engaged after diagnosis, before the action
    incident = drive(orch, clock, {"ESCALATED", "VERIFYING"})
    assert incident["state"] == "ESCALATED" and actuator.applied == []


def test_approval_flow_and_expiry(world):
    tel, clock, store, actuator, make = world
    orch = make(Policy(mode="propose", rules=AUTO.rules))
    incident = drive(orch, clock, {"AWAITING_APPROVAL"})
    clock.now += 5
    orch.tick()
    assert actuator.applied == []                                        # waits for a human
    assert store.decide(incident["id"], "approved", "jay", incident["data"]["plan_hash"], clock.now)
    drive(orch, clock, {"VERIFYING"})
    assert actuator.applied == [("deploy", "checkout", "v1")]


def test_unanswered_approval_expires(world):
    tel, clock, store, actuator, make = world
    orch = make(Policy(mode="propose", rules=AUTO.rules))
    drive(orch, clock, {"AWAITING_APPROVAL"})
    clock.now += TIMING.approval_ttl_s + 1
    bad_release(tel, clock.now - 10, clock.now + 5)
    orch.tick()
    incident = store.list_incidents()[0]
    assert incident["state"] == "ESCALATED" and "expired" in incident["data"]["outcome"]["reason"]


def test_rejected_plan_is_not_executed(world):
    tel, clock, store, actuator, make = world
    orch = make(Policy(mode="propose", rules=AUTO.rules))
    incident = drive(orch, clock, {"AWAITING_APPROVAL"})
    store.decide(incident["id"], "rejected", "jay", incident["data"]["plan_hash"], clock.now)
    assert drive(orch, clock, {"ESCALATED", "VERIFYING"})["state"] == "ESCALATED" and actuator.applied == []


def test_no_duplicate_incident_while_one_is_open_or_just_closed(world):
    tel, clock, store, actuator, make = world
    orch = make(Policy(mode="observe"))
    drive(orch, clock, {"ESCALATED"})
    for _ in range(10):                                                  # still failing: the human has it
        clock.now += 2
        orch.tick()
    assert len(store.list_incidents()) == 1


def test_config_incident_reverts_the_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(prove, "preflight", lambda *a: dict(PASS))
    tel = FakeTelemetry()
    healthy(tel, T0, T0 + 60)
    tel.change("chg-7", T0 + 59.9, "config", "checkout", 800, 5, key="payments.timeout_ms")
    payments_timeouts(tel, T0 + 60, T0 + 200)
    clock, store = Clock(T0 + 64), Store(tmp_path / "db.sqlite")
    actuator = FakeActuator(release="v1", config={"payments.timeout_ms": 5})
    orch = Orchestrator(settings(tmp_path), store, tel, actuator, AUTO, clock=clock)
    drive(orch, clock, {"VERIFYING"})
    assert actuator.applied == [("set_config", "checkout", "payments.timeout_ms", 800)]
