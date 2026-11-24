"""Action policy. Enforced in code, loaded from a TOML file, and fail-closed:
with no policy file the system only proposes, and an unknown action is denied.

    mode = "auto"            # observe | propose | auto
    max_actions_per_hour = 4
    kill_switch_file = "/var/run/lamplighter.stop"

    [actions.rollback_release]
    tier = "auto"            # auto | approval | deny
    services = ["checkout"]  # optional allow-list
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .store import Store

KNOWN_ACTIONS = ("rollback_release", "revert_config", "restart")
MODES = ("observe", "propose", "auto")
TIERS = ("auto", "approval", "deny")


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    tier: str = "deny"
    services: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Policy:
    mode: str = "propose"
    max_actions_per_hour: int = 4
    kill_switch_file: Path | None = None
    rules: dict[str, Rule] = field(default_factory=lambda: {name: Rule("approval") for name in KNOWN_ACTIONS})

    @staticmethod
    def load(path: Path | None) -> "Policy":
        if path is None:
            return Policy()
        try:
            raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise PolicyError(f"cannot read policy {path}: {exc}") from exc
        unknown = set(raw) - {"mode", "max_actions_per_hour", "kill_switch_file", "actions"}
        if unknown:
            raise PolicyError(f"unknown policy keys: {sorted(unknown)}")
        mode = raw.get("mode", "propose")
        if mode not in MODES:
            raise PolicyError(f"mode must be one of {MODES}")
        limit = raw.get("max_actions_per_hour", 4)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 100:
            raise PolicyError("max_actions_per_hour must be an integer from 0 to 100")
        rules = {}
        for name, body in (raw.get("actions") or {}).items():
            if name not in KNOWN_ACTIONS:
                raise PolicyError(f"unknown action {name!r}")
            tier = body.get("tier", "deny")
            if tier not in TIERS:
                raise PolicyError(f"{name}: tier must be one of {TIERS}")
            services = body.get("services")
            if services is not None and not (isinstance(services, list) and all(isinstance(s, str) for s in services)):
                raise PolicyError(f"{name}: services must be a list of names")
            rules[name] = Rule(tier, tuple(services) if services is not None else None)
        kill = raw.get("kill_switch_file")
        return Policy(mode, limit, Path(kill) if kill else None, rules)

    def kill_switch_on(self) -> bool:
        return self.kill_switch_file is not None and self.kill_switch_file.exists()

    def decide(self, plan: dict | None, store: Store, now: float) -> dict:
        """Return {"verdict": auto|approval|deny, "reasons": [...]} for a planned action."""
        if plan is None:
            return {"verdict": "deny", "reasons": ["no safe automated action exists for this diagnosis"]}
        kind, service = plan.get("kind"), plan.get("service")
        rule = self.rules.get(kind)
        if kind not in KNOWN_ACTIONS or rule is None:
            return {"verdict": "deny", "reasons": [f"action {kind!r} is not in the policy"]}
        if self.mode == "observe":
            return {"verdict": "deny", "reasons": ["policy mode is observe: diagnose only"]}
        if self.kill_switch_on():
            return {"verdict": "deny", "reasons": ["kill switch is engaged"]}
        if rule.tier == "deny":
            return {"verdict": "deny", "reasons": [f"{kind} is denied by policy"]}
        if rule.services is not None and service not in rule.services:
            return {"verdict": "deny", "reasons": [f"{kind} is not allowed on {service}"]}
        recent = store.actions_since(now - 3600.0)
        if recent >= self.max_actions_per_hour:
            return {"verdict": "deny", "reasons": [f"rate limit: {recent} actions in the last hour "
                                                   f"(limit {self.max_actions_per_hour})"]}
        if self.mode == "propose" or rule.tier == "approval":
            return {"verdict": "approval", "reasons": [f"{kind} on {service} needs human approval"]}
        return {"verdict": "auto", "reasons": [f"{kind} on {service} is pre-approved by policy"]}
