"""
Events and alerts are stored a batch at a time, in one transaction.

A pass of 500 files is thousands of events. Written one autocommit round trip each, the writer could not keep
up with a feed of ~20 files a second, and a failure part-way left a stored prefix behind.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src import storage
from src.models import AlertEvent, NASEvent


@pytest.fixture
def db():
    storage.override_db_path(":memory:")
    yield storage
    storage.override_db_path(":memory:")


def _event(**kw) -> NASEvent:
    t = datetime.now(timezone.utc)
    fields = dict(airport_code="KDCA", tracon="PCT", service_name="ISMC", message_type="STATUS", event_time_utc=t,
                  received_time_utc=t, latency_ms=10.0, feed_status="operational", validation_status="PASSED",
                  data_origin="live")
    return NASEvent(**{**fields, **kw})


async def _count(db, table: str) -> int:
    return await (await db.get_db_pool()).fetchval(f"SELECT count(*) FROM {table}")


async def test_a_batch_is_stored_whole_and_storing_it_again_changes_nothing(db):
    events = [_event() for _ in range(50)]

    await db.save_events(events)
    await db.save_events(events)

    assert await _count(db, "nas_events") == 50


async def test_the_same_event_twice_in_one_batch_is_stored_once_with_the_last_values(db):
    first = _event(latency_ms=1.0)
    again = first.model_copy(update={"latency_ms": 2.0})

    await db.save_events([first, again])

    pool = await db.get_db_pool()
    assert await pool.fetchval("SELECT latency_ms FROM nas_events WHERE event_id = $1", first.event_id) == 2.0
    assert await _count(db, "nas_events") == 1


async def test_a_batch_with_one_bad_value_stores_none_of_it(db):
    """Not a prefix: the batch is one transaction, and the upsert makes the retry safe."""
    good = [_event() for _ in range(3)]
    # Beyond the INTEGER column. The model now refuses such a value, so it is forced past the model here: this is
    # the storage layer's own guarantee, for whatever else can make one row fail.
    bad = _event().model_copy(update={"altitude_ft": 3_000_000_000})

    with pytest.raises(Exception):                   # noqa: B017 -- asyncpg's DataError, not part of our contract
        await db.save_events([*good, bad])

    assert await _count(db, "nas_events") == 0


async def test_an_empty_batch_is_nothing_to_do(db):
    await db.save_events([])
    await db.save_alerts([])
    assert await _count(db, "nas_events") == 0


async def test_alerts_are_stored_a_batch_at_a_time_and_upserted(db):
    alerts = [AlertEvent(alert_type="ALERT_LOW_RVR", severity="CRITICAL", airport_code="KDCA",
                         service_name="APDS_RVR", description=f"Low RVR {n} ft", data_origin="live")
              for n in range(5)]

    await db.save_alerts(alerts)
    await db.save_alerts([alerts[0].model_copy(update={"description": "changed"})])

    pool = await db.get_db_pool()
    assert await _count(db, "alert_events") == 5
    assert await pool.fetchval("SELECT description FROM alert_events WHERE alert_id = $1", alerts[0].alert_id) == "changed"
