"""End-to-end tests against the real demo shop (real processes, real HTTP). Run with --e2e."""
import json
import time
from dataclasses import replace

import pytest

from demo.shop.controlplane import RELEASES_ROOT
from eval.lab import Lab
from lamplighter import fixforward
from lamplighter.actions import ControlPlaneClient
from lamplighter.fixforward import read_release_file

pytestmark = pytest.mark.e2e

GOOD_PATCH = read_release_file(RELEASES_ROOT, "checkout", "v2", "logic.py").replace(
    '    coupon = body["coupon"]\n', '    coupon = body.get("coupon")\n    if coupon is None:\n        return 0\n')
# serves the failing requests, but silently gives every customer 10% off: plausible-looking and wrong
OVERFIT_PATCH = read_release_file(RELEASES_ROOT, "checkout", "v2", "logic.py").replace(
    '    coupon = body["coupon"]\n', '    coupon = body.get("coupon", "WELCOME10")\n')
HOSTILE_PATCH = "import os\n" + GOOD_PATCH


class ScriptedModel:
    def __init__(self, patch):
        self.patch, self.calls = patch, 0

    def complete(self, system, user, max_tokens=1500):
        self.calls += 1
        if "fix one defect" in system:
            return f"<file>\n{self.patch}</file>"
        return json.dumps({"narrative": [{"text": "The release introduced a new error in checkout.", "evidence": ["E1"]},
                                         {"text": "Exactly 987654 customers were affected.", "evidence": ["E1"]},
                                         {"text": "The moon was full.", "evidence": []}],
                           "next_steps": [{"text": "Add a test for carts without a coupon.", "evidence": ["E1"]}]})


def test_bad_release_is_rolled_back_proven_and_briefed(tmp_path):
    lab = Lab(tmp_path, llm=ScriptedModel(GOOD_PATCH), llm_patch=True).start()
    try:
        started = time.time()
        lab.plane.deploy("checkout", "v2", actor="alice")
        incident = lab.wait_closed(100)
        assert incident is not None and incident["state"] == "RESOLVED"
        data = incident["data"]
        assert data["plan"]["kind"] == "rollback_release" and lab.plane.describe()["releases"]["checkout"] == "v1"
        pre = data["preflight"]
        assert pre["reproduced"] and pre["control"]["failing_reproduced"] == pre["control"]["failing_total"] > 0
        assert pre["fix"]["passed"] and pre["fix"]["golden"]["total"] > 0
        assert [c["healthy"] for c in data["verify"]["checks"]][-2:] == [True, True]
        actions = lab.runtime.store.actions_for(incident["id"])
        assert [(a["kind"], a["status"]) for a in actions] == [("rollback_release", "DONE")]
        brief = data["fixforward"]
        assert brief["culprit"]["in_changed_code"] and 'body["coupon"]' in brief["culprit"]["line_text"]
        assert brief["patch"]["status"] == "plausible" and "+    coupon = body.get(\"coupon\")" in brief["patch"]["diff"]
        assert lab.plane.describe()["releases"]["checkout"] == "v1"          # the patch was NOT deployed
        narrative = data["narrative"]
        assert [s["text"] for s in narrative["narrative"]] == ["The release introduced a new error in checkout."]
        assert narrative["dropped_ungrounded"] == 2
        folder = tmp_path / "incidents" / incident["id"]
        assert (folder / "postmortem.md").is_file() and (folder / "fix-forward.patch").is_file()
        assert lab.runtime.store.verify_audit()[0]
        time.sleep(6)
        after = [r for r in lab.gateway_records() if r["ts"] > incident["closed_at"]]
        assert after and not [r for r in after if r["status"] >= 500]         # customers are fine again
        assert lab.customer_impact(started)["impact_s"] < 20
    finally:
        lab.stop()


def test_wrong_and_hostile_patches_are_rejected_by_replay_and_screen(tmp_path):
    lab = Lab(tmp_path).start(warmup_s=1.0, worker=False)
    try:
        actuator = ControlPlaneClient(lab.plane.url, lab.cp_token, lab.settings.allowed_hosts)
        settings = replace(lab.settings, llm_patch=True)
        cart = {"items": [{"sku": "SKU-3", "qty": 2}]}
        golden = [r for r in lab.gateway_records() if r["status"] == 200][:10]
        cases = {"failing": [{"method": "POST", "route": "/checkout", "req": cart}],
                 "golden": [{"method": "POST", "route": "/checkout", "req": r["req"], "status": 200, "resp": r["resp"]}
                            for r in golden]}
        plan = {"kind": "rollback_release", "service": "checkout", "from": "v2", "to": "v1"}
        signature = {"signature": "KeyError: 'coupon'",
                     "frame": {"file": "logic.py", "line": 34, "func": "_discount_percent"}}

        def status(patch):
            return fixforward.build(settings, plan, signature, cases, actuator, ScriptedModel(patch))["patch"]

        assert status(GOOD_PATCH)["status"] == "plausible"
        overfit = status(OVERFIT_PATCH)
        assert overfit["status"] == "rejected" and overfit["replay"]["failing"]["passed"] == 1   # it "fixes" the error
        assert overfit["replay"]["golden"]["passed"] < overfit["replay"]["golden"]["total"]       # and changes prices
        hostile = status(HOSTILE_PATCH)
        assert hostile["status"] == "rejected" and any("adds imports" in p for p in hostile["problems"])
        assert status("def handle_checkout(:\n")["status"] == "rejected"
        assert lab.plane.describe()["releases"]["checkout"] == "v1"
    finally:
        lab.stop()


def test_lamplighter_crash_mid_incident_resumes_and_acts_exactly_once(tmp_path):
    lab = Lab(tmp_path).start()
    try:
        lab.plane.set_config("checkout", "payments.timeout_ms", 5, actor="bob")
        assert lab.wait_state("GATE", 30) is not None
        lab.restart_runtime()                       # the process dies while holding the lease; a new one starts
        lab.start_worker()
        incident = lab.wait_closed(120)
        assert incident is not None and incident["state"] == "RESOLVED"
        assert lab.plane.describe()["config"]["checkout"]["payments.timeout_ms"] == 800
        changes = [json.loads(line) for line in (tmp_path / "changes.jsonl").read_text().splitlines()]
        assert [c["to"] for c in changes if c["actor"] == "lamplighter"] == [800]
    finally:
        lab.stop()
