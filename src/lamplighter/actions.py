"""The only place that changes the outside world.

Actions come from a fixed catalogue. Their parameters are taken from change
history and the service catalogue, never from model output. Every mutating call
carries an idempotency key, which makes retrying after a lost response safe.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Protocol

from .config import open_url, validate_url


class ActionError(Exception):
    """The control plane refused the request; retrying will not help."""


class TransientError(Exception):
    """The request may not have arrived or the control plane is busy; retry with the same key."""


class Actuator(Protocol):
    def state(self) -> dict: ...
    def deploy(self, service: str, release: str, key: str) -> dict: ...
    def set_config(self, service: str, key_name: str, value: Any, key: str) -> dict: ...
    def restart(self, service: str, key: str) -> dict: ...
    def sandbox_create(self, releases: dict | None, config: dict | None, override_files: dict | None = None) -> dict: ...
    def sandbox_delete(self, sandbox_id: str) -> None: ...


class ControlPlaneClient:
    def __init__(self, base_url: str, token: str, allowed_hosts: frozenset[str], timeout_s: float = 30.0,
                 actor: str = "lamplighter") -> None:
        self._base = validate_url(base_url, allowed_hosts, "control plane URL")
        self._token = token
        self._timeout = timeout_s
        self._actor = actor

    def _request(self, method: str, path: str, body: dict | None = None, retries: int = 0) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        last: Exception | None = None
        for attempt in range(retries + 1):
            request = urllib.request.Request(self._base + path, data=data, method=method, headers={
                "Authorization": f"Bearer {self._token}", "Content-Type": "application/json"})
            try:
                with open_url(request, self._timeout) as resp:
                    payload = json.loads(resp.read())
                    if not isinstance(payload, dict):
                        raise ActionError("control plane returned a non-object")
                    return payload
            except urllib.error.HTTPError as exc:
                detail = exc.read(2048).decode("utf-8", "replace")
                if exc.code >= 500:
                    last = TransientError(f"control plane {exc.code}: {detail}")
                else:
                    raise ActionError(f"control plane {exc.code}: {detail}") from None
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last = TransientError(f"control plane unreachable: {exc}")
            except json.JSONDecodeError:
                last = TransientError("control plane returned invalid JSON")
            if attempt < retries:
                time.sleep(min(2.0, 0.25 * 2 ** attempt))
        assert last is not None
        raise last

    def state(self) -> dict:
        return self._request("GET", "/state", retries=2)

    def deploy(self, service: str, release: str, key: str) -> dict:
        return self._request("POST", "/deploy", {"service": service, "release": release, "idempotency_key": key,
                                                 "actor": self._actor}, retries=2)

    def set_config(self, service: str, key_name: str, value: Any, key: str) -> dict:
        return self._request("POST", "/config", {"service": service, "key": key_name, "value": value,
                                                 "idempotency_key": key, "actor": self._actor}, retries=2)

    def restart(self, service: str, key: str) -> dict:
        return self._request("POST", "/restart", {"service": service, "idempotency_key": key,
                                                  "actor": self._actor}, retries=2)

    def sandbox_create(self, releases: dict | None, config: dict | None, override_files: dict | None = None) -> dict:
        return self._request("POST", "/sandbox", {"releases": releases or {}, "config": config or {},
                                                  "override_files": override_files or {}})

    def sandbox_delete(self, sandbox_id: str) -> None:
        if not sandbox_id.isalnum():
            raise ActionError("invalid sandbox id")
        self._request("DELETE", f"/sandbox/{sandbox_id}")


def execute(actuator: Actuator, action: dict, key: str) -> dict:
    """Run one catalogue action (or an inverse). Unknown kinds are refused."""
    kind = action.get("kind")
    if kind == "rollback_release":
        return actuator.deploy(action["service"], action["to"], key)
    if kind == "deploy":
        return actuator.deploy(action["service"], action["release"], key)
    if kind == "revert_config":
        return actuator.set_config(action["service"], action["key"], action["to"], key)
    if kind == "set_config":
        return actuator.set_config(action["service"], action["key"], action["value"], key)
    if kind == "restart":
        return actuator.restart(action["service"], key)
    raise ActionError(f"unknown action kind {kind!r}")


def precondition(actuator: Actuator, plan: dict) -> tuple[bool, str]:
    """The world must still look the way the diagnosis saw it. A plan made
    against an older state (someone already rolled back, changed the value
    again) is stale and must not run."""
    state = actuator.state()
    kind, service = plan["kind"], plan["service"]
    if kind == "rollback_release":
        current = state.get("releases", {}).get(service)
        return current == plan["from"], f"{service} is running {current}; the plan expects {plan['from']}"
    if kind == "revert_config":
        current = state.get("config", {}).get(service, {}).get(plan["key"])
        return current == plan["from"], f"{plan['key']} is {current}; the plan expects {plan['from']}"
    if kind == "restart":
        return True, "restart has no state precondition"
    return False, f"unknown action kind {kind!r}"
