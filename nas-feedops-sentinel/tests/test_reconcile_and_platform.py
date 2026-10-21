"""
The escalation-integrity reconciler. (How services get their secrets -- no
AppRole, no token -- is pinned by tests/test_vault_agent_file_source.py and
instraclustr/tests. The edge TLS certificate is tests/test_identity_tls.py's.)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import storage
from src.config import settings
from src.models import AlertEvent


@pytest.fixture
def db():
    storage.override_db_path(":memory:")
    yield storage
    storage.override_db_path(":memory:")


def _critical(minutes_ago: float, **kw) -> AlertEvent:
    return AlertEvent(alert_type=kw.pop("alert_type", "ALERT_FEED_DISCONNECTED"), severity="CRITICAL",
                      airport_code="KDCA", service_name="ISMC", description="reconcile test",
                      triggered_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago), **kw)


# ── Reconciler ────────────────────────────────────────────────────────────────

async def test_reconciler_stages_recent_and_records_old_unstaged_alerts(db, monkeypatch):
    """The state the live database was in: CRITICAL alerts saved, never staged."""
    from src.metrics import CRITICAL_ALERTS_UNSTAGED
    from src.reconcile import reconcile_unstaged

    monkeypatch.setattr(settings, "RECONCILE_STAGE_WINDOW_MINUTES", 60)
    recent, old = _critical(5), _critical(60 * 24 * 30, alert_type="ALERT_LOW_RVR")
    await db.save_alerts([recent, old])          # saved like the old dashboard path did: no staging

    summary = await reconcile_unstaged()
    assert summary == {"staged_pending": 1, "recorded_expired": 1, "still_unstaged": 0, "merged": 0, "stale_feeds": 0}
    assert CRITICAL_ALERTS_UNSTAGED._value.get() == 0

    pending = await db.get_pending_staged_actions()
    assert [p.alert_id for p in pending] == [recent.alert_id]
    assert await db.get_ticket_by_action_id(pending[0].action_id) is not None

    by_alert = {a.alert_id: a for a in await db.get_recent_staged_actions(limit=10)}
    expired = by_alert[old.alert_id]
    assert expired.status == "EXPIRED"
    audit = await db.get_action_audit_log(action_id=expired.action_id)
    assert audit[0]["new_status"] == "EXPIRED" and "never staged" in audit[0]["note"]


async def test_reconciler_is_idempotent(db):
    from src.reconcile import reconcile_unstaged

    await db.save_alerts([_critical(5), _critical(60 * 48)])
    await reconcile_unstaged()
    assert await reconcile_unstaged() == {"staged_pending": 0, "recorded_expired": 0, "still_unstaged": 0, "merged": 0, "stale_feeds": 0}


async def test_reconciler_leaves_already_staged_alerts_alone(db):
    from src.execution_gate import stage_critical_alert
    from src.reconcile import reconcile_unstaged

    alert = _critical(5)
    await db.save_alerts([alert])
    await stage_critical_alert(alert)
    assert (await reconcile_unstaged())["staged_pending"] == 0


# ── An airport that goes quiet ────────────────────────────────────────────────

def test_a_quiet_feed_raises_an_airports_gauge_and_never_lowers_it():
    """The pipeline sets the gauge only when a batch has that airport in it; silence needs the reconciler."""
    from src.metrics import AIRPORT_OVERALL_STATUS, raise_airport_status, set_airport_status

    def level(airport):
        return AIRPORT_OVERALL_STATUS.labels(airport=airport)._value.get()

    set_airport_status("KQT1", "OK")
    raise_airport_status("KQT1", "STALE")
    assert level("KQT1") == 1

    set_airport_status("KQT2", "DEGRADED")
    raise_airport_status("KQT2", "STALE")
    assert level("KQT2") == 2, "a worse status is not lowered by the reconciler"


async def test_the_reconciler_marks_the_airport_of_a_stale_feed(db, monkeypatch):
    from src.metrics import AIRPORT_OVERALL_STATUS, set_airport_status
    from src.models import NASEvent
    from src.reconcile import reconcile_unstaged

    when = datetime.now(timezone.utc) - timedelta(seconds=settings.STALE_FEED_SECONDS + 120)
    await db.save_events([NASEvent(airport_code="KDCA", tracon="PCT", service_name="ISMC", message_type="STATUS",
                                   event_time_utc=when, received_time_utc=when, latency_ms=1.0,
                                   feed_status="operational", validation_status="PASSED", data_origin="live")])
    set_airport_status("KDCA", "OK")               # the last thing a pipeline batch said about it

    summary = await reconcile_unstaged()

    assert summary["stale_feeds"] == 1
    assert AIRPORT_OVERALL_STATUS.labels(airport="KDCA")._value.get() == 1
