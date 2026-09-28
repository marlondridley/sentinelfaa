"""
The reads the Overview verdict is built from can be limited to one data origin, so a replay or a
synthetic run cannot speak for the live feed. Without the filter they see everything, as before.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import storage
from src.execution_gate import stage_critical_alert
from src.models import AlertEvent, NASEvent


@pytest.fixture(autouse=True)
def db():
    storage.override_db_path(":memory:")
    yield
    storage.override_db_path(":memory:")


def _event(origin: str, validation: str = "PASSED", latency_ms: float = 100.0) -> NASEvent:
    t = datetime.now(timezone.utc)
    return NASEvent(airport_code="KDCA", tracon="PCT", service_name="ISMC", message_type="STATUS",
                    event_time_utc=t, received_time_utc=t, latency_ms=latency_ms, feed_status="operational",
                    validation_status=validation, data_origin=origin)


def _alert(origin: str) -> AlertEvent:
    return AlertEvent(alert_type="ALERT_LOW_RVR", severity="CRITICAL", airport_code="KDCA", service_name="APDS_RVR",
                      description="Low RVR 600 ft at KDCA", data_origin=origin)


async def test_events_can_be_read_by_origin():
    await storage.save_events([_event("live"), _event("live"), _event("replay")])

    assert len(await storage.get_recent_events(limit=10)) == 3
    assert {e["data_origin"] for e in await storage.get_recent_events(limit=10, data_origin="live")} == {"live"}
    assert len(await storage.get_recent_events(limit=10, data_origin="live")) == 2
    assert await storage.get_recent_events(limit=10, data_origin="synthetic") == []


async def test_the_limit_applies_after_the_origin_so_a_replay_cannot_crowd_out_live_rows():
    await storage.save_events([_event("live"), *(_event("replay") for _ in range(5))])
    assert len(await storage.get_recent_events(limit=3, data_origin="live")) == 1


async def test_validation_and_latency_can_be_read_by_origin():
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    await storage.save_events([_event("live", latency_ms=100.0),
                               _event("replay", validation="FAILED", latency_ms=90_000.0),
                               _event("replay", validation="FAILED", latency_ms=90_000.0)])

    everything = await storage.get_validation_summary(since)
    live = await storage.get_validation_summary(since, data_origin="live")
    assert (everything["total"], everything["failed"]) == (3, 2)
    assert (live["total"], live["passed"], live["failed"]) == (1, 1, 0)
    assert (await storage.get_latency_stats(since, data_origin="live"))["avg_ms"] == pytest.approx(100.0)
    assert (await storage.get_latency_stats(since))["avg_ms"] > 50_000


async def test_alerts_can_be_read_by_origin():
    await storage.save_alerts([_alert("live"), _alert("replay"), _alert("synthetic")])

    assert len(await storage.get_recent_alerts(limit=10)) == 3
    assert [a["data_origin"] for a in await storage.get_recent_alerts(limit=10, data_origin="live")] == ["live"]


async def test_pending_reviews_can_be_read_by_origin():
    live, replay = _alert("live"), _alert("replay")
    await storage.save_alerts([live, replay])
    await stage_critical_alert(live)
    await stage_critical_alert(replay)

    assert len(await storage.get_pending_staged_actions()) == 2
    [only] = await storage.get_pending_staged_actions(data_origin="live")
    assert only.alert_id == live.alert_id


async def test_a_review_with_no_alert_on_record_is_kept_in_the_live_view():
    """Unattributable, so it can only make a verdict worse, never hide a problem."""
    from src.models import StagedAction

    await storage.create_staged_action(StagedAction(alert_type="ALERT_LOW_RVR", airport_code="KDCA",
                                                    service_name="APDS_RVR", idempotency_key="no-alert"))
    assert len(await storage.get_pending_staged_actions(data_origin="live")) == 1
