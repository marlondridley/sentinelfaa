"""
VaultSettingsSource retries a transient Vault fetch failure with backoff before
raising VaultConfigError -- added after a live incident where several services
crash-looped in a tight, un-backed-off cycle against a Vault mid-credential-
rotation. Each retry is a FRESH login (src/vault_config.py's own docstring),
not a retry of stale client state, and every attempt is logged with the real
underlying error in the message text itself, not only in `extra`, since a
prior version of this code logged `extra={"error": ...}` fields that never
actually appeared in the JSON-formatted output -- the real cause of an entire
incident's worth of "Required secret(s) missing" with no visible reason.

time.sleep is monkeypatched everywhere here: these tests prove attempt counts
and backoff arguments, not wall-clock delay.
"""
from __future__ import annotations

import types

import pytest

import src.config
import src.vault_config as vault_config
from src.vault_config import VaultConfigError, VaultSettingsSource


@pytest.fixture(autouse=True)
def fast_and_isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(vault_config.time, "sleep", lambda seconds: None)
    monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
    monkeypatch.setenv("VAULT_APPROLE_DIR", str(tmp_path))
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    (tmp_path / "role_id").write_text("role-123\n")
    (tmp_path / "secret_id").write_text("secret-456\n")


def _fake_hvac(read_secret_version, login=lambda **kw: None):
    return types.SimpleNamespace(Client=lambda url, token=None: types.SimpleNamespace(
        auth=types.SimpleNamespace(approle=types.SimpleNamespace(login=login)),
        secrets=types.SimpleNamespace(kv=types.SimpleNamespace(v2=types.SimpleNamespace(
            read_secret_version=read_secret_version))),
    ))


def _complete_secrets(path):
    return {"data": {"data": {
        "postgres/credentials": {"username": "owner", "password": "pw", "database": "sentinel_db"},
        "sentinel/config": {"LITELLM_API_KEY": "sk-sentinel-virtual"},
    }.get(path, {})}}


def test_a_login_that_fails_then_succeeds_is_retried_not_fatal(monkeypatch):
    attempts = []

    def login(**kw):
        attempts.append(1)
        if len(attempts) < 3:
            raise Exception("invalid role or secret ID")

    monkeypatch.setattr(vault_config, "hvac", _fake_hvac(_complete_secrets, login))

    values = VaultSettingsSource(src.config.Settings)()

    assert len(attempts) == 3, "the first two logins failed; the third succeeded"
    assert values["LITELLM_API_KEY"] == "sk-sentinel-virtual"


def test_a_missing_required_secret_is_retried_with_a_fresh_login_each_time(monkeypatch):
    responses = iter([{}, {}, {"LITELLM_API_KEY": "sk-sentinel-virtual"}])
    logins = []

    def read_secret_version(path):
        if path == "sentinel/config":
            return {"data": {"data": next(responses)}}
        return {"data": {"data": {}}}

    monkeypatch.setattr(vault_config, "hvac", _fake_hvac(
        read_secret_version, login=lambda **kw: logins.append(kw)))

    values = VaultSettingsSource(src.config.Settings)()

    assert len(logins) == 3, "a missing key retries the whole fetch, including the login, not just the read"
    assert values["LITELLM_API_KEY"] == "sk-sentinel-virtual"


def test_exhausting_every_attempt_raises_with_the_real_reason_not_just_missing(monkeypatch):
    def login(**kw):
        raise Exception("invalid role or secret ID")

    monkeypatch.setattr(vault_config, "hvac", _fake_hvac(_complete_secrets, login))

    with pytest.raises(VaultConfigError, match="invalid role or secret ID"):
        VaultSettingsSource(src.config.Settings)()


def test_the_backoff_grows_and_stops_one_short_of_the_attempt_count(monkeypatch):
    sleeps = []
    monkeypatch.setattr(vault_config.time, "sleep", sleeps.append)

    def login(**kw):
        raise Exception("down")

    monkeypatch.setattr(vault_config, "hvac", _fake_hvac(_complete_secrets, login))

    with pytest.raises(VaultConfigError):
        VaultSettingsSource(src.config.Settings)()

    assert len(sleeps) == vault_config._MAX_ATTEMPTS - 1, "no sleep after the last, failed attempt"
    assert sleeps == sorted(sleeps) and len(set(sleeps)) > 1, "backoff actually grows"


def test_every_attempts_real_error_reaches_the_log_message_text(monkeypatch, caplog):
    """
    The bug this whole module exists to fix: extra={"error": ...} never
    appeared in this project's JSON log output (src/logging_config.py's
    JsonFormatter only serializes the fields named in its own fmt string), so
    a whole incident's logs said only "vault_client_init_failed" with the
    real reason nowhere to be found. The reason must be in the message text
    itself, which every formatter renders unconditionally.
    """
    def login(**kw):
        raise Exception("a distinctive, searchable reason")

    monkeypatch.setattr(vault_config, "hvac", _fake_hvac(_complete_secrets, login))

    with caplog.at_level("WARNING", logger="src.vault_config"):
        with pytest.raises(VaultConfigError):
            VaultSettingsSource(src.config.Settings)()

    assert any("a distinctive, searchable reason" in r.getMessage() for r in caplog.records)


def test_a_healthy_first_attempt_does_not_sleep_at_all(monkeypatch):
    monkeypatch.setattr(vault_config.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("should not sleep")))
    monkeypatch.setattr(vault_config, "hvac", _fake_hvac(_complete_secrets))

    values = VaultSettingsSource(src.config.Settings)()

    assert values["LITELLM_API_KEY"] == "sk-sentinel-virtual"
