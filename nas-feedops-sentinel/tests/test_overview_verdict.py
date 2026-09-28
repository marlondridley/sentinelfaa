"""
The Overview verdict answers "can we trust the feed right now?", so it has to
notice when the feed goes quiet. It used to be computed from lifetime
aggregates and the newest 500 alerts of any age: a feed that stopped an hour
ago left every one of those unchanged, and the banner still said GREEN.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import config, storage
from src.models import NASEvent


@pytest.fixture(autouse=True)
def isolated_db():
    storage.override_db_path(":memory:")
    yield
    storage.override_db_path(":memory:")


def _event(age_s: float, airport: str = "KDCA", origin: str = "live", validation: str = "PASSED") -> NASEvent:
    t = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    return NASEvent(airport_code=airport, tracon="PCT", service_name="ISMC", message_type="STATUS",
                    event_time_utc=t, received_time_utc=t, latency_ms=100.0, feed_status="operational",
                    validation_status=validation, data_origin=origin)


def _overview() -> str:
    pytest.importorskip("streamlit", reason="the Overview renders through Streamlit")
    from streamlit.testing.v1 import AppTest

    app = Path(__file__).resolve().parent.parent / "dashboard" / "app.py"
    at = AppTest.from_file(str(app), default_timeout=90).run()
    assert not at.exception, [e.value for e in at.exception]
    # The page's explanation ("The GREEN/AMBER/RED verdict above ...") names every colour; it is not the verdict.
    return " ".join(m.value for m in at.markdown if "GREEN/AMBER/RED" not in m.value)


async def test_a_current_feed_is_green():
    await storage.save_events([_event(5)])
    assert "GREEN" in _overview()


async def test_a_feed_stale_within_the_hour_is_amber_not_green():
    await storage.save_events([_event(config.STALE_FEED_SECONDS + 120)])
    html = _overview()
    assert "AMBER" in html and "GREEN" not in html


async def test_a_feed_silent_for_hours_is_no_data_not_green():
    await storage.save_events([_event(3 * 3600)])
    html = _overview()
    assert "NO DATA" in html and "GREEN" not in html


# ── the verdict is about the LIVE feed ────────────────────────────────────────
#
# A replay lands in the same tables with a receipt time of now. Counted, it made a dead feed read
# GREEN off replayed captures, or a healthy one read RED off a replayed fault.

async def test_replayed_captures_do_not_make_a_silent_live_feed_green():
    await storage.save_events([_event(3 * 3600), *(_event(5, origin="replay") for _ in range(20))])
    html = _overview()
    assert "NO DATA" in html and "GREEN" not in html
    assert "20 replay" in html and "not counted" in html, "it says where the fresh data came from"


async def test_replayed_faults_do_not_turn_a_healthy_live_feed_red():
    from src.execution_gate import stage_critical_alert
    from src.models import AlertEvent

    await storage.save_events([_event(5), *(_event(5, origin="replay", validation="FAILED") for _ in range(30))])
    replayed = AlertEvent(alert_type="ALERT_LOW_RVR", severity="CRITICAL", airport_code="KDCA", service_name="ISMC",
                          description="Low RVR 600 ft at KDCA", data_origin="replay")
    await storage.save_alerts([replayed])
    assert await stage_critical_alert(replayed) is not None, "a replay review is real work; it is just not the live verdict"

    html = _overview()
    assert "GREEN" in html and not re.search(r"\bRED\b", html)


async def test_a_live_fault_still_turns_the_verdict_red_whatever_a_replay_says():
    from src.execution_gate import stage_critical_alert
    from src.models import AlertEvent

    await storage.save_events([_event(5), *(_event(5, origin="replay") for _ in range(30))])
    live = AlertEvent(alert_type="ALERT_LOW_RVR", severity="CRITICAL", airport_code="KDCA", service_name="ISMC",
                      description="Low RVR 600 ft at KDCA", data_origin="live")
    await storage.save_alerts([live])
    await stage_critical_alert(live)

    assert re.search(r"\bRED\b", _overview())


async def test_a_replay_cannot_freshen_a_stale_live_feed():
    """The airport's feed status is read from its latest events; a replayed one is not the feed."""
    await storage.save_events([
        _event(5, airport="KDCA"),
        _event(config.STALE_FEED_SECONDS + 120, airport="KIAD"),
        *(_event(5, airport="KIAD", origin="replay") for _ in range(10)),
    ])
    html = _overview()
    assert "AMBER" in html and "GREEN" not in html
