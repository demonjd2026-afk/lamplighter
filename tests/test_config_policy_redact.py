import pytest

from lamplighter.config import LOOPBACK_HOSTS, ConfigError, Settings, validate_url
from lamplighter.policy import Policy, PolicyError
from lamplighter.redact import redact, redact_text
from lamplighter.store import Store

PLAN = {"kind": "rollback_release", "service": "checkout", "from": "v2", "to": "v1"}


@pytest.mark.parametrize("url", [
    "http://example.com/x",             # host not allowed
    "http://user:pw@127.0.0.1/",        # credentials in the URL
    "file:///etc/passwd", "gopher://127.0.0.1", "127.0.0.1:80", "",
    "http://169.254.169.254/latest",    # cloud metadata
    "http://127.0.0.1.evil.test/",
])
def test_urls_outside_the_allow_list_are_refused(url):
    with pytest.raises(ConfigError):
        validate_url(url, LOOPBACK_HOSTS, "test")


def test_plain_http_is_refused_off_loopback_even_when_host_is_allowed():
    with pytest.raises(ConfigError):
        validate_url("http://cp.internal/x", LOOPBACK_HOSTS | {"cp.internal"}, "test")
    assert validate_url("https://cp.internal/x/", LOOPBACK_HOSTS | {"cp.internal"}, "test") == "https://cp.internal/x"


def test_settings_reject_weak_or_missing_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("LAMPLIGHTER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LAMPLIGHTER_CONTROLPLANE_URL", "http://127.0.0.1:9000")
    monkeypatch.delenv("LAMPLIGHTER_CONTROLPLANE_TOKEN", raising=False)
    with pytest.raises(ConfigError):
        Settings.from_env()
    monkeypatch.setenv("LAMPLIGHTER_CONTROLPLANE_TOKEN", "c" * 32)
    monkeypatch.setenv("LAMPLIGHTER_API_TOKEN", "short")
    with pytest.raises(ConfigError):
        Settings.from_env()
    monkeypatch.setenv("LAMPLIGHTER_API_TOKEN", "a" * 32)
    monkeypatch.setenv("LAMPLIGHTER_LLM_PROVIDER", "anthropic")
    with pytest.raises(ConfigError):  # a provider without a model and key
        Settings.from_env()
    monkeypatch.delenv("LAMPLIGHTER_LLM_PROVIDER")
    loaded = Settings.from_env()
    assert "c" * 32 not in repr(loaded) and "a" * 32 not in repr(loaded)  # secrets never appear in repr


def test_policy_defaults_fail_closed(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    assert Policy().decide(PLAN, store, 0.0)["verdict"] == "approval"   # no file: propose only
    assert Policy().decide(None, store, 0.0)["verdict"] == "deny"
    assert Policy().decide({"kind": "drop_database", "service": "x"}, store, 0.0)["verdict"] == "deny"


def test_policy_tiers_allow_lists_rate_limit_and_kill_switch(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    kill = tmp_path / "stop"
    path = tmp_path / "policy.toml"
    path.write_text(f'mode = "auto"\nmax_actions_per_hour = 1\nkill_switch_file = "{kill}"\n'
                    '[actions.rollback_release]\ntier = "auto"\nservices = ["checkout"]\n'
                    '[actions.restart]\ntier = "approval"\n')
    policy = Policy.load(path)
    assert policy.decide(PLAN, store, 100.0)["verdict"] == "auto"
    assert policy.decide({**PLAN, "service": "billing"}, store, 100.0)["verdict"] == "deny"
    assert policy.decide({"kind": "restart", "service": "checkout"}, store, 100.0)["verdict"] == "approval"
    assert policy.decide({"kind": "revert_config", "service": "checkout"}, store, 100.0)["verdict"] == "deny"
    kill.write_text("stop")
    assert policy.decide(PLAN, store, 100.0)["verdict"] == "deny"
    kill.unlink()
    store.action_begin("k", "inc", "restart", {}, 90.0)
    store.action_finish("k", "DONE", {}, 91.0)
    assert policy.decide(PLAN, store, 100.0)["verdict"] == "deny"       # rate limit reached
    assert policy.decide(PLAN, store, 90.0 + 3601)["verdict"] == "auto"  # an hour later


@pytest.mark.parametrize("text", ['mode = "yolo"', 'surprise = 1', '[actions.rm_rf]\ntier = "auto"',
                                  '[actions.restart]\ntier = "always"', 'max_actions_per_hour = -1', 'mode = ['])
def test_policy_rejects_anything_it_does_not_understand(tmp_path, text):
    path = tmp_path / "policy.toml"
    path.write_text(text)
    with pytest.raises(PolicyError):
        Policy.load(path)


def test_redaction_masks_secrets_and_identifiers():
    text = ("Authorization: Bearer abcdefghijklmnop123 key sk-abcdefghijklmnopqrstuv user a.b@example.com "
            "card 4111 1111 1111 1111 phone +91 9876543210 password=hunter2 AKIAABCDEFGHIJKLMNOP")
    out = redact_text(text)
    for secret in ("abcdefghijklmnop123", "sk-abcdefghijklmnopqrstuv", "a.b@example.com", "4111 1111 1111 1111",
                   "9876543210", "hunter2", "AKIAABCDEFGHIJKLMNOP"):
        assert secret not in out
    nested = redact({"api_key": "zzz", "items": [{"note": "mail me at x@y.io", "qty": 2}]})
    assert nested == {"api_key": "[REDACTED]", "items": [{"note": "mail me at [REDACTED_EMAIL]", "qty": 2}]}
