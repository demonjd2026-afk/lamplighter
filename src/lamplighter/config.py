"""Settings. Everything is read from the environment once, validated, and frozen."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlparse

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Timing:
    """Windows and thresholds. Defaults follow the multi-window burn-rate practice
    for a fast-burn page (5 minute and 1 hour windows, burn rate 14.4)."""

    short_window_s: float = 300.0
    long_window_s: float = 3600.0
    burn_threshold: float = 14.4
    min_requests: int = 50          # do not judge a window with less traffic than this
    bucket_s: float = 10.0          # resolution of the onset search
    lookback_s: float = 3600.0      # how far back the onset search may go
    baseline_s: float = 1800.0      # healthy period compared against the incident
    change_lag_s: float = 1800.0    # a change older than this before onset is not a suspect
    hold_s: float = 120.0           # an alert must persist this long before any action
    recent_window_s: float = 60.0   # "is it still failing right now?"
    settle_s: float = 30.0          # ignore telemetry this long after an action
    verify_window_s: float = 60.0
    verify_checks: int = 3          # consecutive healthy windows needed as proof
    verify_timeout_s: float = 600.0
    verify_max_bad: float = 0.01
    verify_min_requests: int = 20
    approval_ttl_s: float = 900.0
    cooldown_s: float = 600.0
    max_step_attempts: int = 4

    @staticmethod
    def demo() -> "Timing":
        """The same logic with windows scaled to seconds, for the local demo."""
        return Timing(short_window_s=10, long_window_s=30, burn_threshold=10, min_requests=30, bucket_s=1,
                      lookback_s=120, baseline_s=45, change_lag_s=90, hold_s=6, recent_window_s=4,
                      settle_s=2, verify_window_s=4, verify_checks=2, verify_timeout_s=40,
                      verify_max_bad=0.02, verify_min_requests=20, approval_ttl_s=120, cooldown_s=20)


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "off", ""):
        return False
    raise ConfigError(f"{name} must be a boolean")


def _secret(name: str, minimum: int = 24) -> str:
    value = os.environ.get(name, "")
    if value and len(value) < minimum:
        raise ConfigError(f"{name} must be at least {minimum} characters")
    return value


def validate_url(url: str, allowed_hosts: frozenset[str], what: str) -> str:
    """Only http(s) URLs to explicitly allowed hosts; https required off loopback."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError(f"{what}: not an http(s) URL")
    if parsed.username or parsed.password:
        raise ConfigError(f"{what}: credentials in the URL are not allowed")
    if parsed.hostname not in allowed_hosts:
        raise ConfigError(f"{what}: host {parsed.hostname!r} is not in the allowed list")
    if parsed.scheme == "http" and parsed.hostname not in LOOPBACK_HOSTS:
        raise ConfigError(f"{what}: plain http is only allowed on loopback")
    return url.rstrip("/")


@dataclass(frozen=True)
class Settings:
    data_dir: Path                       # telemetry root (JSONL adapter) and incident artefacts
    db_path: Path
    controlplane_url: str
    controlplane_token: str = field(repr=False)
    api_token: str = field(default="", repr=False)        # approve/reject; empty disables those routes
    webhook_secret: str = field(default="", repr=False)   # HMAC for POST /v1/alerts; empty disables it
    public_read: bool = False
    bind_host: str = "127.0.0.1"
    bind_port: int = 8088
    policy_file: Path | None = None
    releases_root: Path | None = None    # source of releases, for the fix-forward brief
    allowed_hosts: frozenset[str] = LOOPBACK_HOSTS
    noise_keys: frozenset[str] = frozenset({"order_id", "auth_id", "ts"})
    llm_provider: str = ""               # "", "anthropic" or "openai"
    llm_model: str = ""
    llm_api_key: str = field(default="", repr=False)
    llm_base_url: str = ""
    llm_api_version: str = "2026-10-21"  # value of the anthropic-version header
    llm_patch: bool = False              # let the model propose a code patch (sandbox-verified)
    notify_url: str = ""
    timing: Timing = field(default_factory=Timing)

    def with_timing(self, timing: Timing) -> "Settings":
        return replace(self, timing=timing)

    @staticmethod
    def from_env() -> "Settings":
        env = os.environ
        data_dir = Path(env.get("LAMPLIGHTER_DATA_DIR", "./data")).resolve()
        extra_hosts = frozenset(h.strip() for h in env.get("LAMPLIGHTER_ALLOWED_HOSTS", "").split(",") if h.strip())
        allowed = LOOPBACK_HOSTS | extra_hosts
        token = _secret("LAMPLIGHTER_CONTROLPLANE_TOKEN", 16)
        if not token:
            raise ConfigError("LAMPLIGHTER_CONTROLPLANE_TOKEN is required")
        provider = env.get("LAMPLIGHTER_LLM_PROVIDER", "").lower()
        if provider not in ("", "anthropic", "openai"):
            raise ConfigError("LAMPLIGHTER_LLM_PROVIDER must be anthropic, openai or empty")
        model, api_key = env.get("LAMPLIGHTER_LLM_MODEL", ""), env.get("LAMPLIGHTER_LLM_API_KEY", "")
        base_url = env.get("LAMPLIGHTER_LLM_BASE_URL", "")
        if provider:
            if not model or not api_key:
                raise ConfigError("an LLM provider needs LAMPLIGHTER_LLM_MODEL and LAMPLIGHTER_LLM_API_KEY")
            if provider == "anthropic" and not base_url:
                base_url = "https://api.anthropic.com"
            if provider == "openai" and not base_url:
                base_url = "https://api.openai.com/v1"
            host = urlparse(base_url).hostname or ""
            base_url = validate_url(base_url, allowed | {host}, "LAMPLIGHTER_LLM_BASE_URL")
        notify = env.get("LAMPLIGHTER_NOTIFY_URL", "")
        if notify:
            notify = validate_url(notify, allowed | {urlparse(notify).hostname or ""}, "LAMPLIGHTER_NOTIFY_URL")
        profile = env.get("LAMPLIGHTER_PROFILE", "production")
        if profile not in ("production", "demo"):
            raise ConfigError("LAMPLIGHTER_PROFILE must be production or demo")
        policy = env.get("LAMPLIGHTER_POLICY_FILE")
        releases = env.get("LAMPLIGHTER_RELEASES_ROOT")
        try:
            port = int(env.get("LAMPLIGHTER_PORT", "8088"))
        except ValueError:
            raise ConfigError("LAMPLIGHTER_PORT must be an integer") from None
        return Settings(
            data_dir=data_dir,
            db_path=Path(env.get("LAMPLIGHTER_DB", str(data_dir / "lamplighter.db"))).resolve(),
            controlplane_url=validate_url(env.get("LAMPLIGHTER_CONTROLPLANE_URL", ""), allowed,
                                          "LAMPLIGHTER_CONTROLPLANE_URL"),
            controlplane_token=token,
            api_token=_secret("LAMPLIGHTER_API_TOKEN"),
            webhook_secret=_secret("LAMPLIGHTER_WEBHOOK_SECRET"),
            public_read=_bool("LAMPLIGHTER_PUBLIC_READ"),
            bind_host=env.get("LAMPLIGHTER_HOST", "127.0.0.1"),
            bind_port=port,
            policy_file=Path(policy).resolve() if policy else None,
            releases_root=Path(releases).resolve() if releases else None,
            allowed_hosts=allowed,
            llm_provider=provider, llm_model=model, llm_api_key=api_key, llm_base_url=base_url,
            llm_api_version=env.get("LAMPLIGHTER_ANTHROPIC_VERSION", "2026-10-21"),
            llm_patch=_bool("LAMPLIGHTER_LLM_PATCH"),
            notify_url=notify,
            timing=Timing.demo() if profile == "demo" else Timing(),
        )
