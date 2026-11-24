"""Detection, evidence and diagnosis on synthetic telemetry with known ground truth."""
from demo.shop.controlplane import CATALOG
from lamplighter.detect import evaluate, still_failing
from lamplighter.evidence import collect, find_onset, normalise
from lamplighter.reason import accepted_hypothesis, diagnose
from tests.helpers import SLO, T0, TIMING, FakeTelemetry, bad_release, healthy, inventory_hang, payments_timeouts


def run(tel, now):
    bundle = collect(tel, "gateway", SLO, now, TIMING)
    return bundle, diagnose(bundle, CATALOG, TIMING)


def test_client_errors_never_page():
    tel = FakeTelemetry()
    for i in range(600):
        tel.request("gateway", T0 + i / 10, status=400 if i % 2 else 422)
    assert evaluate(tel, "gateway", SLO, T0 + 60, TIMING) is None


def test_alert_needs_both_windows_and_enough_traffic():
    tel = FakeTelemetry()
    healthy(tel, T0, T0 + 60)
    assert evaluate(tel, "gateway", SLO, T0 + 60, TIMING) is None
    bad_release(tel, T0 + 60, T0 + 61.5)                    # 1.5 s of failures: long window not yet over threshold
    assert evaluate(tel, "gateway", SLO, T0 + 61.5, TIMING) is None
    bad_release(tel, T0 + 61.5, T0 + 65)
    alert = evaluate(tel, "gateway", SLO, T0 + 65, TIMING)
    assert alert is not None and alert["burn_short"] >= TIMING.burn_threshold
    quiet = FakeTelemetry()
    for i in range(5):                                      # five failures are not a trend
        quiet.request("gateway", T0 + i, status=500)
    assert evaluate(quiet, "gateway", SLO, T0 + 5, TIMING) is None


def test_slow_requests_count_against_a_latency_objective():
    tel = FakeTelemetry()
    for i in range(400):
        tel.request("gateway", T0 + i / 10, status=200, latency=SLO["latency_ms"] + 500)
    assert evaluate(tel, "gateway", SLO, T0 + 40, TIMING) is not None


def test_still_failing_uses_the_freshest_window():
    tel = FakeTelemetry()
    healthy(tel, T0, T0 + 30)
    bad_release(tel, T0 + 30, T0 + 40)
    healthy(tel, T0 + 40, T0 + 46)
    assert still_failing(tel, "gateway", SLO, T0 + 40, TIMING)[0]
    assert not still_failing(tel, "gateway", SLO, T0 + 46, TIMING)[0]


def test_onset_is_the_first_failure_of_the_latest_run():
    tel = FakeTelemetry()
    healthy(tel, T0, T0 + 20)
    bad_release(tel, T0 + 20, T0 + 23)      # an earlier, separate blip
    healthy(tel, T0 + 23, T0 + 60)
    bad_release(tel, T0 + 60, T0 + 70)
    onset = find_onset(tel.requests("gateway", T0, T0 + 70), SLO, T0, T0 + 70, TIMING)
    assert abs(onset - (T0 + 60)) < 0.2


def test_bad_release_is_diagnosed_with_a_rollback_plan():
    tel = FakeTelemetry()
    healthy(tel, T0, T0 + 60)
    tel.change("chg-1", T0 + 59.8, "deploy", "checkout", "v1", "v2")
    bad_release(tel, T0 + 60, T0 + 66)
    bundle, diagnosis = run(tel, T0 + 66)
    hyp = accepted_hypothesis(diagnosis)
    assert (hyp["component"], hyp["cause_class"]) == ("checkout", "bad_release")
    assert hyp["plan"] == {"kind": "rollback_release", "service": "checkout", "from": "v2", "to": "v1",
                           "inverse": {"kind": "deploy", "service": "checkout", "release": "v2"}}
    cited = {e for check in hyp["checks"] for e in check["evidence"]}
    assert cited and cited <= bundle.ids()          # every citation points at real evidence


def test_recent_deploy_is_not_blamed_when_a_dependency_hangs():
    tel = FakeTelemetry()
    healthy(tel, T0, T0 + 50)
    tel.change("chg-1", T0 + 50, "deploy", "checkout", "v1", "v1_1")   # the red herring
    healthy(tel, T0 + 50, T0 + 60, release="v1_1")
    inventory_hang(tel, T0 + 60, T0 + 66, release="v1_1")
    _, diagnosis = run(tel, T0 + 66)
    hyp = accepted_hypothesis(diagnosis)
    assert (hyp["component"], hyp["cause_class"], hyp["plan"]["kind"]) == ("inventory", "service_degraded", "restart")
    release = next(h for h in diagnosis["hypotheses"] if h["cause_class"] == "bad_release")
    assert not release["accepted"]


def test_low_timeout_is_blamed_on_the_config_change_not_the_healthy_dependency():
    tel = FakeTelemetry()
    healthy(tel, T0, T0 + 60)
    tel.change("chg-7", T0 + 59.9, "config", "checkout", 800, 5, key="payments.timeout_ms")
    payments_timeouts(tel, T0 + 60, T0 + 66)
    _, diagnosis = run(tel, T0 + 66)
    hyp = accepted_hypothesis(diagnosis)
    assert (hyp["component"], hyp["cause_class"]) == ("checkout", "bad_config")
    assert hyp["plan"]["to"] == 800 and hyp["plan"]["key"] == "payments.timeout_ms"
    assert all(c["passed"] for c in hyp["checks"])          # including "5ms is below the normal p95"


def test_a_change_made_after_failures_began_cannot_be_the_cause():
    tel = FakeTelemetry()
    healthy(tel, T0, T0 + 60)
    bad_release(tel, T0 + 60, T0 + 66, release="v1")
    tel.change("chg-1", T0 + 63, "deploy", "checkout", "v0", "v1")
    _, diagnosis = run(tel, T0 + 66)
    release = next(h for h in diagnosis["hypotheses"] if h["cause_class"] == "bad_release")
    assert not release["accepted"]
    assert not next(c for c in release["checks"] if c["name"] == "change precedes onset")["passed"]


def test_a_superseded_release_is_not_rolled_back():
    tel = FakeTelemetry()
    healthy(tel, T0, T0 + 60)
    tel.change("chg-1", T0 + 59.8, "deploy", "checkout", "v1", "v2")
    bad_release(tel, T0 + 60, T0 + 66)
    tel.change("chg-2", T0 + 65, "deploy", "checkout", "v2", "v3")     # a human already moved on
    _, diagnosis = run(tel, T0 + 66)
    assert diagnosis["verdict"] == "none"


def test_errors_without_any_change_are_reported_but_not_acted_on():
    tel = FakeTelemetry()
    healthy(tel, T0, T0 + 60)
    bad_release(tel, T0 + 60, T0 + 66, release="v1")        # same failure shape, no change event at all
    _, diagnosis = run(tel, T0 + 66)
    hyp = accepted_hypothesis(diagnosis)
    assert hyp["cause_class"] == "unexplained_errors" and hyp["plan"] is None


def test_our_own_remediation_is_never_a_suspect():
    tel = FakeTelemetry()
    healthy(tel, T0, T0 + 60)
    tel.change("chg-9", T0 + 59.8, "deploy", "checkout", "v2", "v1", actor="lamplighter")
    bad_release(tel, T0 + 60, T0 + 66, release="v1")
    _, diagnosis = run(tel, T0 + 66)
    assert all(h["cause_class"] != "bad_release" for h in diagnosis["hypotheses"])


def test_no_telemetry_gives_no_diagnosis_and_does_not_crash():
    _, diagnosis = run(FakeTelemetry(), T0)
    assert diagnosis["verdict"] == "none" and diagnosis["accepted"] is None


def test_error_signatures_ignore_variable_parts():
    assert normalise("timeout after 812ms for order 3f9a1c2b7d4e5f60") == normalise("timeout after 5ms for order aaaaaaaaaaaa1111")
