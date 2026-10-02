"""
The migrate job (src/storage/migrate.py): refusing a role name that is not a plain identifier, and the order
of what the job does: schema, migrations, the application role, its grants. The database is replaced.
"""
from __future__ import annotations

import pytest

import src.storage.migrate as job


@pytest.mark.parametrize("role", ["", "Robert'); DROP TABLE x;--", "has space", "UPPER", "1leading", "a" * 64])
async def test_a_role_name_that_is_not_a_plain_identifier_is_refused_before_any_sql(role):
    class _Conn:
        async def fetchval(self, *a, **k):
            raise AssertionError("SQL ran for an invalid role name")

        execute = fetchval

    with pytest.raises(ValueError, match="invalid role name"):
        await job.ensure_app_role(_Conn(), role, "pw")
    with pytest.raises(ValueError, match="invalid role name"):
        await job.apply_grants(_Conn(), role)


class _Conn:
    def __init__(self, role_exists: bool):
        self.role_exists = role_exists
        self.calls: list[str] = []

    async def fetchval(self, sql, *args):
        self.calls.append("fetchval")
        return 1 if self.role_exists else None


class _Pool:
    def __init__(self, conn):
        self.conn, self.closed = conn, False

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def close(self):
        self.closed = True


class _Harness:
    """What the job did, in order, and the pool its create_pool hands out."""

    def __init__(self, role_exists: bool = False) -> None:
        self.log: list[str] = []
        self.pool = _Pool(_Conn(role_exists))


@pytest.fixture
def harness(monkeypatch):
    h = _Harness()

    async def _create_pool(**kwargs):
        h.log.append("pool")
        return h.pool

    async def _init(pool):
        h.log.append("schema")

    async def _migrations(pool):
        h.log.append("migrations")

    async def _ensure(conn, role, password):
        h.log.append(f"role:{role}")

    async def _grants(conn, role):
        h.log.append(f"grants:{role}")

    monkeypatch.setattr(job.asyncpg, "create_pool", _create_pool)
    monkeypatch.setattr(job, "init_schema", _init)
    monkeypatch.setattr(job, "run_migrations", _migrations)
    monkeypatch.setattr(job, "ensure_app_role", _ensure)
    monkeypatch.setattr(job, "apply_grants", _grants)
    return h


async def test_the_job_builds_the_schema_then_the_role_then_its_grants_and_closes_the_pool(harness):
    harness.pool = _Pool(_Conn(role_exists=True))

    await job.migrate("sentinel_app", "secret")

    assert harness.log == ["pool", "schema", "migrations", "role:sentinel_app", "grants:sentinel_app"]
    assert harness.pool.closed


async def test_without_a_role_the_job_only_migrates(harness):
    await job.migrate(None, None)

    assert harness.log == ["pool", "schema", "migrations"] and harness.pool.closed


async def test_a_role_that_does_not_exist_gets_no_grants(harness):
    await job.migrate("sentinel_app", None)      # no password: the role is not created, and so not granted

    assert harness.log == ["pool", "schema", "migrations"] and harness.pool.closed


async def test_the_pool_is_closed_even_when_a_migration_fails(harness, monkeypatch):
    async def _boom(pool):
        raise RuntimeError("migration failed")

    monkeypatch.setattr(job, "run_migrations", _boom)

    with pytest.raises(RuntimeError, match="migration failed"):
        await job.migrate(None, None)
    assert harness.pool.closed


def test_main_hands_the_settings_to_the_job(monkeypatch):
    seen: dict = {}

    async def _migrate(role, password):
        seen.update(role=role, password=password)

    monkeypatch.setattr(job, "migrate", _migrate)
    monkeypatch.setattr(job, "configure_logging", lambda: None)
    monkeypatch.setattr(job.settings, "APP_DB_USER", "sentinel_app")
    monkeypatch.setattr(job.settings, "APP_DB_PASSWORD", "")

    job.main()

    assert seen == {"role": "sentinel_app", "password": None}, "an empty password is None, so no role is created"
