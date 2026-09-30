"""
The schema a fresh database ends up with, and the pool that setup opens.
"""
from __future__ import annotations

import pytest

from src import storage
from src.storage import pool as pool_module
from src.storage.schema import _MIGRATIONS

_DUPLICATED_FKS = """
    SELECT conrelid::regclass::text AS child, confrelid::regclass::text AS parent, count(*) AS n
    FROM pg_constraint WHERE contype = 'f'
    GROUP BY conrelid, confrelid, conkey, confkey HAVING count(*) > 1
"""


@pytest.fixture
def db():
    storage.override_db_path(":memory:")
    yield storage
    storage.override_db_path(":memory:")


async def test_no_foreign_key_exists_twice(db):
    """A fresh database had each one inline in CREATE TABLE and again from the referential-integrity migration."""
    pool = await db.get_db_pool()
    assert await pool.fetch(_DUPLICATED_FKS) == []


async def test_the_cleanup_migration_drops_a_duplicate_and_keeps_the_original(db):
    pool = await db.get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("""ALTER TABLE alert_events ADD CONSTRAINT fk_alert_events_event_again
                              FOREIGN KEY (event_id) REFERENCES nas_events(event_id) ON DELETE SET NULL""")
        assert len(await conn.fetch(_DUPLICATED_FKS)) == 1

        cleanup = dict(_MIGRATIONS)["2026_09_drop_duplicate_foreign_keys"]
        await conn.execute(cleanup)

        assert await conn.fetch(_DUPLICATED_FKS) == []
        remaining = await conn.fetch("""SELECT conname FROM pg_constraint
                                        WHERE contype = 'f' AND conrelid = 'alert_events'::regclass
                                          AND confrelid = 'nas_events'::regclass""")
        assert [r["conname"] for r in remaining] == ["alert_events_event_id_fkey"]


async def test_a_pool_whose_schema_setup_fails_is_closed_not_leaked(monkeypatch):
    closed: list[bool] = []

    class FakePool:
        async def close(self):
            closed.append(True)

    async def fake_create_pool(**_kw):
        return FakePool()

    async def failing_init(_pool):
        raise RuntimeError("schema setup failed")

    monkeypatch.setattr(pool_module.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(pool_module, "init_schema", failing_init)
    monkeypatch.setattr(pool_module.settings, "RUN_MIGRATIONS_ON_CONNECT", True)
    await pool_module.close_db_pool()

    with pytest.raises(RuntimeError, match="schema setup failed"):
        await pool_module.get_db_pool()

    assert closed == [True]
    assert pool_module._POOLS.get(__import__("asyncio").get_running_loop()) is None
