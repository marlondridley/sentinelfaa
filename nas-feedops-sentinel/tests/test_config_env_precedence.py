"""
Regression tests for environment-variable precedence in src/config.py.

These exist because of a specific incident: config.py used to delete every
os.environ entry whose value was the empty string, at import time. That made
the documented way to aim the test suite at a throwaway database --

    docker run ... -e DATABASE_URL= -e POSTGRES_DB=sentinel_test ...

-- resolve instead to .env's nas-pgbouncer:6432/sentinel_db, the live
database: the emptied DATABASE_URL was removed from the environment, so the
.env value took over. A full-suite run reached
src/storage/test_support.reset_db() aimed at production data, and only the
current_database() guard there stopped the TRUNCATE.

An empty environment variable is the only way to say "ignore .env, use the
computed default" from a container's command line, so these tests pin that
behaviour down.

They reload src.config with the overrides already in the environment, which
is the only faithful reproduction: the deletion happened at *import* time, so
a test that sets the variables after import never sees the bug. Each reload
is undone afterwards, restoring both os.environ and the module singleton, and
nothing here touches a database.
"""
from __future__ import annotations

import contextlib
import importlib
import os

import pytest

import src.config


# The overrides src/storage/test_support.py tells operators to use, verbatim.
TEST_DB_OVERRIDES = {
    "VAULT_ADDR": "",
    "VAULT_TOKEN": "",
    "POSTGRES_HOST": "postgres",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "sentinel_test",
    "DATABASE_URL": "",
}


@contextlib.contextmanager
def config_imported_with(**overrides: str):
    """
    Reload src.config with ``overrides`` present in os.environ, exactly as a
    container would see them, and yield the reloaded module.

    Restores the environment and re-imports the module on the way out so the
    src.config.settings singleton other modules hold is left as it was found.
    """
    saved = dict(os.environ)
    try:
        os.environ.update(overrides)
        yield importlib.reload(src.config)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        importlib.reload(src.config)


def test_importing_config_does_not_mutate_the_process_environment():
    """Importing config must not delete empty variables it does not own."""
    with config_imported_with(SENTINEL_UNRELATED_EMPTY_VAR=""):
        assert os.environ.get("SENTINEL_UNRELATED_EMPTY_VAR") == ""


def test_empty_database_url_rebuilds_dsn_from_postgres_settings():
    """-e DATABASE_URL= must mean 'rebuild from POSTGRES_*', not 'read .env'."""
    with config_imported_with(**TEST_DB_OVERRIDES) as config:
        settings = config.settings

        assert settings.POSTGRES_DB == "sentinel_test"
        assert settings.POSTGRES_HOST == "postgres"
        assert settings.POSTGRES_PORT == 5432
        assert settings.DATABASE_URL.endswith("@postgres:5432/sentinel_test")


def test_documented_overrides_never_resolve_to_the_live_database():
    """The failure this whole file exists to prevent."""
    with config_imported_with(**TEST_DB_OVERRIDES) as config:
        assert "nas-pgbouncer" not in config.settings.DATABASE_URL
        assert not config.settings.DATABASE_URL.endswith("/sentinel_db")


def test_empty_vault_address_disables_the_vault_settings_source():
    """
    VaultSettingsSource outranks the environment, so if an empty VAULT_ADDR did
    not disable it, it would seed POSTGRES_DB from secret/postgres/credentials
    and put the suite back on sentinel_db.
    """
    with config_imported_with(**TEST_DB_OVERRIDES) as config:
        assert config.settings.VAULT_ADDR == ""
        assert not config.settings.VAULT_TOKEN
        assert config.settings.POSTGRES_DB == "sentinel_test"


# ── Collection-typed settings ────────────────────────────────────────────────
#
# docker-compose's `- API_KEYS=${API_KEYS:-}` idiom passes an empty string when
# the variable is not set. For list/set/dict fields, pydantic-settings tries to
# JSON-decode the environment value before any validator runs, so "" crashed
# Settings() at import -- which took nas-api down in a restart loop. (The old
# import-time deletion of empty variables had been masking this.)

COLLECTION_FIELDS = ["API_KEYS", "CORS_ALLOWED_ORIGINS", "SUPPORTED_AIRPORTS", "SUPPORTED_SERVICES"]


@pytest.mark.parametrize("field", COLLECTION_FIELDS)
def test_empty_collection_setting_falls_back_to_its_default(field):
    """
    An empty string cannot mean an empty collection: that is how compose
    spells "not configured", and an empty SUPPORTED_AIRPORTS would silently
    reject every event. It means "use the default".
    """
    with config_imported_with(**TEST_DB_OVERRIDES, **{field: ""}) as config:
        default = config.Settings.model_fields[field].get_default(call_default_factory=True)
        assert getattr(config.settings, field) == default


def test_collection_settings_still_parse_json_and_comma_lists():
    overrides = {
        **TEST_DB_OVERRIDES,
        "API_KEYS": '{"k-1": "ingest-service"}',
        "CORS_ALLOWED_ORIGINS": "http://a.example, http://b.example",
        "SUPPORTED_AIRPORTS": '["KDCA", "KIAD"]',
    }
    with config_imported_with(**overrides) as config:
        assert config.settings.API_KEYS == {"k-1": "ingest-service"}
        assert config.settings.CORS_ALLOWED_ORIGINS == ["http://a.example", "http://b.example"]
        assert config.settings.SUPPORTED_AIRPORTS == {"KDCA", "KIAD"}


def test_api_keys_that_is_not_a_json_object_is_rejected():
    """A malformed key map must fail loudly, not silently authenticate nobody."""
    import pydantic

    with pytest.raises((pydantic.ValidationError, ValueError)):
        with config_imported_with(**TEST_DB_OVERRIDES, API_KEYS="not-json"):
            pass
