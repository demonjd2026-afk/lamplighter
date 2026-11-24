import sqlite3

import pytest

from lamplighter.store import Store


def test_audit_chain_detects_tampering(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    for i in range(5):
        store.audit("inc-x", "event", {"n": i})
    assert store.verify_audit() == (True, 5, None)
    store.close()
    raw = sqlite3.connect(tmp_path / "db.sqlite")
    raw.execute("UPDATE audit SET payload='{\"n\":99}' WHERE seq=3")
    raw.commit()
    raw.close()
    intact, _, bad = Store(tmp_path / "db.sqlite").verify_audit()
    assert not intact and bad == 3


def test_audit_chain_detects_deletion(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    for i in range(4):
        store.audit(None, "event", {"n": i})
    store.close()
    raw = sqlite3.connect(tmp_path / "db.sqlite")
    raw.execute("DELETE FROM audit WHERE seq=2")
    raw.commit()
    raw.close()
    assert Store(tmp_path / "db.sqlite").verify_audit()[0] is False


def test_one_open_incident_per_service(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    first = store.open_incident("gateway", {"a": 1}, 10.0)
    assert first is not None
    assert store.open_incident("gateway", {"a": 2}, 11.0) is None
    assert store.open_incident("other", {"a": 3}, 11.0) is not None
    store.transition(first, "ESCALATED", "closed", {}, 12.0)
    assert store.open_incident("gateway", {"a": 4}, 13.0) is not None


def test_closed_incident_cannot_transition(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    incident = store.open_incident("gateway", {}, 1.0)
    store.transition(incident, "RESOLVED", "closed", {}, 2.0)
    with pytest.raises(RuntimeError):
        store.transition(incident, "ACTING", "zombie", {}, 3.0)
    with pytest.raises(ValueError):
        store.transition(store.open_incident("gateway", {}, 4.0), "NOT_A_STATE", "x", {}, 5.0)


def test_action_intent_is_recorded_once(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    assert store.action_begin("k1", "inc", "restart", {"service": "a"}, 1.0)["status"] == "PENDING"
    assert store.action_begin("k1", "inc", "restart", {"service": "a"}, 2.0)["status"] == "PENDING"
    store.action_finish("k1", "DONE", {"ok": True}, 3.0)
    again = store.action_begin("k1", "inc", "restart", {"service": "a"}, 4.0)
    assert again["status"] == "DONE" and again["result"] == {"ok": True}
    assert len(store.actions_for("inc")) == 1 and store.actions_since(0.0) == 1


def test_decision_needs_matching_plan_and_state(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    incident = store.open_incident("gateway", {}, 1.0)
    incident["data"]["plan_hash"] = "a" * 64
    assert not store.decide(incident["id"], "approved", "me", "a" * 64, 2.0)  # not awaiting approval yet
    store.transition(incident, "AWAITING_APPROVAL", "approval_requested", {}, 2.0)
    assert not store.decide(incident["id"], "approved", "me", "b" * 64, 3.0)  # a different plan
    assert store.decide(incident["id"], "approved", "me", "a" * 64, 3.0)
    assert not store.decide(incident["id"], "rejected", "you", "a" * 64, 4.0)  # first decision wins
    assert store.approval(incident["id"])["decision"] == "approved"


def test_lease_excludes_second_worker_until_expiry(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    assert store.lease("a", 30, 100.0)
    assert not store.lease("b", 30, 110.0)
    assert store.lease("a", 30, 120.0)
    assert store.lease("b", 30, 151.0)
