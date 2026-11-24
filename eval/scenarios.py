"""Fault scenarios with ground truth. Each one injects a real fault into the running
demo shop; the expectation says what a correct on-call response is."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from eval.lab import Lab


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    inject: Callable[[Lab], float]   # injects the fault and returns when it began
    root: str | None          # true root-cause component
    cause: str | None         # true cause class
    action: str | None        # the correct automated action, or None when a human must decide
    final: tuple[str, ...]    # acceptable final incident states; ("NONE",) means no incident should open
    watch_s: float = 0.0      # for no-incident scenarios: how long to watch


def _bad_release(lab: Lab) -> float:
    started = time.time()
    lab.plane.deploy("checkout", "v2", actor="alice")
    return started


def _bad_config(lab: Lab) -> float:
    started = time.time()
    lab.plane.set_config("checkout", "payments.timeout_ms", 5, actor="bob")
    return started


def _dependency_stuck(lab: Lab) -> float:
    lab.plane.deploy("checkout", "v1_1", actor="carol")  # harmless deploy shortly before: a red herring
    time.sleep(8)
    started = time.time()
    lab.plane.inject_fault("inventory", "stuck")
    return started


def _process_crash(lab: Lab) -> float:
    started = time.time()
    lab.plane.kill("checkout")
    return started


def _external_dependency(lab: Lab) -> float:
    started = time.time()
    lab.plane.inject_fault("payments", "errors")
    return started


def _restart_ineffective(lab: Lab) -> float:
    started = time.time()
    lab.plane.inject_fault("inventory", "stuck", persistent=True)
    return started


def _transient_blip(lab: Lab) -> float:
    started = time.time()
    lab.plane.inject_fault("inventory", "stuck", ttl_s=4.0)
    return started


def _client_error_burst(lab: Lab) -> float:
    started = time.time()
    lab.traffic.invalid_fraction = 0.6

    def restore() -> None:
        time.sleep(15)
        lab.traffic.invalid_fraction = 0.04

    threading.Thread(target=restore, daemon=True).start()
    return started


SCENARIOS = [
    Scenario("bad_release", "A release with a code defect", _bad_release,
             "checkout", "bad_release", "rollback_release", ("RESOLVED",)),
    Scenario("bad_config", "A timeout set far too low", _bad_config,
             "checkout", "bad_config", "revert_config", ("RESOLVED",)),
    Scenario("dependency_stuck", "A dependency hangs just after an unrelated deploy", _dependency_stuck,
             "inventory", "service_degraded", "restart", ("RESOLVED",)),
    Scenario("process_crash", "A service process dies", _process_crash,
             "checkout", "process_down", "restart", ("RESOLVED",)),
    Scenario("external_dependency", "A third-party provider fails", _external_dependency,
             "payments", "external_dependency", None, ("ESCALATED",)),
    Scenario("restart_ineffective", "A hang that a restart does not clear", _restart_ineffective,
             "inventory", "service_degraded", "restart", ("ESCALATED",)),
    Scenario("transient_blip", "A four-second hang that clears by itself", _transient_blip,
             None, None, None, ("NONE", "SELF_RESOLVED"), watch_s=30.0),
    Scenario("client_error_burst", "A burst of invalid requests from clients", _client_error_burst,
             None, None, None, ("NONE",), watch_s=25.0),
]
BY_NAME = {s.name: s for s in SCENARIOS}
