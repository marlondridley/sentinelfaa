"""
data_origin is constrained in the database, not only in the model.

The origin is what stops a replay, a test event or an API post being read as the live feed. The column was a free
TEXT with a default, so a write that bypassed the model (a script, a repository call, a misspelling) stored whatever
it was given, and that row then matched no filter at all.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import get_args

import asyncpg
import pytest

from src import storage
from src.models import AlertEvent, AlertOrigin, DataOrigin, NASEvent
from src.storage.pool import get_db_pool
from src.storage.schema import run_migrations


@pytest.fixture(autouse=True)
def isolated_db():
    storage.override_db_path(":memory:")
    yield
    storage.override_db_path(":memory:")


def _event(**kw) -> NASEvent:
    now = datetime.now(timezone.utc)
    fields = dict(airport_code="KDCA", tracon="PCT", service_name="ISMC", message_type="m", event_time_utc=now,
                  received_time_utc=now, latency_ms=1.0, feed_status="operational")
    return NASEvent(**{**fields, **kw})


async def test_every_origin_the_model_allows_is_accepted():
    for origin in get_args(DataOrigin):
        await storage.save_events([_event(data_origin=origin)])
    for origin in get_args(AlertOrigin):
        await storage.save_alert(AlertEvent(alert_type="ALERT_X", severity="WARNING", description="d", data_origin=origin))
    pool = await get_db_pool()
    assert await pool.fetchval("SELECT count(DISTINCT data_origin) FROM nas_events") == len(get_args(DataOrigin))
    assert await pool.fetchval("SELECT count(DISTINCT data_origin) FROM alert_events") == len(get_args(AlertOrigin))


async def test_an_origin_outside_the_vocabulary_is_refused_for_events_and_alerts():
    # Past the model (model_copy does not validate): this is the write the database now has to catch itself.
    bad_event = _event().model_copy(update={"data_origin": "lvie"})
    with pytest.raises(asyncpg.CheckViolationError, match="nas_events_data_origin_check"):
        await storage.save_events([bad_event])
    bad_alert = AlertEvent(alert_type="ALERT_X", severity="WARNING", description="d").model_copy(update={"data_origin": "demo"})
    with pytest.raises(asyncpg.CheckViolationError, match="alert_events_data_origin_check"):
        await storage.save_alert(bad_alert)


async def test_mixed_is_an_alert_origin_only():
    with pytest.raises(asyncpg.CheckViolationError):
        await storage.save_events([_event().model_copy(update={"data_origin": "mixed"})])


async def test_an_empty_and_a_null_origin_are_refused_too():
    pool = await get_db_pool()
    for value in ("", "LIVE"):
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("INSERT INTO nas_events (event_id, data_origin) VALUES ('x', $1)", value)
    with pytest.raises(asyncpg.NotNullViolationError):
        await pool.execute("INSERT INTO nas_events (event_id, data_origin) VALUES ('y', NULL)")


async def test_the_constraints_are_validated_so_they_describe_every_row():
    pool = await get_db_pool()
    rows = await pool.fetch(
        "SELECT conname, convalidated FROM pg_constraint WHERE conname IN "
        "('nas_events_data_origin_check', 'alert_events_data_origin_check')")
    assert {r["conname"]: r["convalidated"] for r in rows} == {
        "nas_events_data_origin_check": True, "alert_events_data_origin_check": True}


async def test_the_migration_sets_rows_already_outside_the_vocabulary_to_unknown():
    pool = await get_db_pool()
    await storage.save_events([_event(event_id="good", data_origin="live")])
    # Put the database back as it was before the migration, with a stray row in it.
    await pool.execute("ALTER TABLE nas_events DROP CONSTRAINT nas_events_data_origin_check")
    await pool.execute("ALTER TABLE alert_events DROP CONSTRAINT alert_events_data_origin_check")
    await pool.execute("INSERT INTO nas_events (event_id, data_origin) VALUES ('stray', 'lvie')")
    await pool.execute("INSERT INTO alert_events (alert_id, data_origin) VALUES ('stray-alert', 'demo')")
    await pool.execute("DELETE FROM schema_migrations WHERE id = '2026_10_data_origin_check'")

    await run_migrations(pool)

    assert await pool.fetchval("SELECT data_origin FROM nas_events WHERE event_id = 'stray'") == "unknown"
    assert await pool.fetchval("SELECT data_origin FROM alert_events WHERE alert_id = 'stray-alert'") == "unknown"
    assert await pool.fetchval("SELECT data_origin FROM nas_events WHERE event_id = 'good'") == "live", "valid rows are untouched"
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("INSERT INTO nas_events (event_id, data_origin) VALUES ('after', 'lvie')")
