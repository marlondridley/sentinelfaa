"""
VaultAgentFileSettingsSource reads secrets Vault Agent already rendered to
disk via SPIFFE-based auto-auth (see instraclustr/vault-agent/*.hcl) -- it
performs no Vault authentication of its own, unlike VaultSettingsSource
(test_vault_retry.py), which it sits alongside until every consumer's
compose service has the sidecars (Phase 4's cutover, not yet done -- see
that class's docstring in src/vault_config.py).

The only retry here is for the file-not-rendered-yet race at container
startup: Agent's own auto_auth + template loop can take a beat after its
sidecar starts. time.sleep is monkeypatched everywhere: these tests prove
polling behavior and content, not wall-clock delay.
"""
from __future__ import annotations

import pytest

import src.config
import src.vault_config as vault_config
from src.vault_config import VaultAgentFileSettingsSource, VaultConfigError


@pytest.fixture(autouse=True)
def fast_and_isolated(monkeypatch):
    monkeypatch.setattr(vault_config.time, "sleep", lambda seconds: None)
    monkeypatch.delenv("VAULT_AGENT_ENV_FILE", raising=False)


def _write(path, **kv):
    path.write_text("".join(f"{k}={v}\n" for k, v in kv.items()), encoding="utf-8")


def test_missing_env_file_setting_disables_the_source(monkeypatch):
    # VAULT_AGENT_ENV_FILE deliberately unset (the autouse fixture already
    # delenv's it) -- this container's compose service has no sidecars yet.
    values = VaultAgentFileSettingsSource(src.config.Settings)()

    assert values == {}


def test_a_ready_file_is_read_immediately_with_no_polling(monkeypatch, tmp_path):
    env_file = tmp_path / "sentinel-config.env"
    _write(env_file, LITELLM_API_KEY="sk-agent-rendered")
    monkeypatch.setenv("VAULT_AGENT_ENV_FILE", str(env_file))
    monkeypatch.setattr(vault_config.time, "sleep",
                         lambda s: (_ for _ in ()).throw(AssertionError("should not poll")))

    values = VaultAgentFileSettingsSource(src.config.Settings)()

    assert values["LITELLM_API_KEY"] == "sk-agent-rendered"


def test_a_file_that_appears_after_a_few_polls_is_picked_up(monkeypatch, tmp_path):
    env_file = tmp_path / "sentinel-config.env"  # does not exist yet
    polls = []

    def fake_sleep(seconds):
        polls.append(seconds)
        if len(polls) == 2:  # Agent "finishes rendering" mid-wait
            _write(env_file, LITELLM_API_KEY="sk-agent-rendered")

    monkeypatch.setenv("VAULT_AGENT_ENV_FILE", str(env_file))
    monkeypatch.setattr(vault_config.time, "sleep", fake_sleep)

    values = VaultAgentFileSettingsSource(src.config.Settings)()

    assert values["LITELLM_API_KEY"] == "sk-agent-rendered"
    assert len(polls) == 2, "the file appeared on the 3rd check, after 2 waits"


def test_a_file_missing_the_required_secret_is_retried_not_trusted_as_is(monkeypatch, tmp_path):
    env_file = tmp_path / "sentinel-config.env"
    _write(env_file, POSTGRES_USER="sentinel_app")  # LITELLM_API_KEY absent
    writes = []

    def fake_sleep(seconds):
        writes.append(1)
        if len(writes) == 1:
            _write(env_file, POSTGRES_USER="sentinel_app", LITELLM_API_KEY="sk-agent-rendered")

    monkeypatch.setenv("VAULT_AGENT_ENV_FILE", str(env_file))
    monkeypatch.setattr(vault_config.time, "sleep", fake_sleep)

    values = VaultAgentFileSettingsSource(src.config.Settings)()

    assert values["LITELLM_API_KEY"] == "sk-agent-rendered"


def test_exhausting_every_attempt_raises_with_the_real_reason(monkeypatch, tmp_path, caplog):
    env_file = tmp_path / "never-rendered.env"  # never created
    monkeypatch.setenv("VAULT_AGENT_ENV_FILE", str(env_file))

    with caplog.at_level("WARNING", logger="src.vault_config"):
        with pytest.raises(VaultConfigError, match="not yet rendered"):
            VaultAgentFileSettingsSource(src.config.Settings)()

    assert any("not yet rendered" in r.getMessage() for r in caplog.records), (
        "the real reason must reach the rendered log message text, not only extra="
    )


def test_parser_skips_blank_lines_and_comments_and_splits_on_first_equals(tmp_path):
    env_file = tmp_path / "sentinel-config.env"
    env_file.write_text(
        "\n"
        # Has an '=' -- only the comment guard, not the blank/no-'=' skip,
        # keeps this out of the result. Proves the guard actually does
        # something, not just that lines without '=' get dropped anyway.
        "# POSTGRES_PASSWORD=should-be-ignored\n"
        "LITELLM_API_KEY=sk-has=an=equals-sign\n"
        "   \n"
        "POSTGRES_USER = sentinel_app \n",  # stray spaces around '='
        encoding="utf-8",
    )

    values = vault_config._parse_agent_env_file(env_file)

    assert values == {
        "LITELLM_API_KEY": "sk-has=an=equals-sign",
        "POSTGRES_USER": "sentinel_app",
    }


# -- What the deployed services actually get ----------------------------------

def test_the_rendered_file_outranks_the_environment(monkeypatch, tmp_path):
    """A stale password left in an environment or .env never wins over Vault's."""
    env_file = tmp_path / "app-config.env"
    _write(env_file, POSTGRES_USER="sentinel_app", POSTGRES_PASSWORD="from-vault",
           LITELLM_API_KEY="sk-agent-rendered")
    monkeypatch.setenv("VAULT_AGENT_ENV_FILE", str(env_file))
    monkeypatch.setenv("POSTGRES_PASSWORD", "stale-from-environment")

    settings = src.config.Settings()

    assert settings.POSTGRES_PASSWORD == "from-vault"
    assert settings.LITELLM_API_KEY == "sk-agent-rendered"


def test_migrate_job_takes_the_app_role_password_from_the_rendered_file(monkeypatch, tmp_path):
    """
    The migrate job's environment holds no password (docker-compose.yml): the
    application role it creates gets its password from the migrate sidecar's
    file (instraclustr/vault-agent/migrate.hcl), through Settings.
    """
    import src.storage.migrate as migrate_job

    env_file = tmp_path / "app-config.env"
    _write(env_file, POSTGRES_USER="sentinel_user", POSTGRES_PASSWORD="owner-from-vault",
           APP_DB_USER="sentinel_app", APP_DB_PASSWORD="app-from-vault",
           LITELLM_API_KEY="sk-agent-rendered")
    monkeypatch.setenv("VAULT_AGENT_ENV_FILE", str(env_file))
    monkeypatch.delenv("APP_DB_PASSWORD", raising=False)
    monkeypatch.setattr(migrate_job, "settings", src.config.Settings())
    called = {}

    async def fake_migrate(role, password):
        called["args"] = (role, password)

    monkeypatch.setattr(migrate_job, "migrate", fake_migrate)
    migrate_job.main()

    assert called["args"] == ("sentinel_app", "app-from-vault")
