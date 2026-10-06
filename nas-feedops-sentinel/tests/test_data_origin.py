"""
Data provenance: every event says where it came from, and nothing derived
from test or replayed data can pass for the live FAA feed.

Replays and API posts write to the same tables as the live feed. Before
data_origin, a synthetic CRITICAL alert and a real one were indistinguishable
in the review queue.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import storage
from src.alert_engine import check_alerts
from src.config import settings
from src.models import NASEvent

CAPTURE = Path(__file__).parent / "fixtures" / "swim_kit" / "capture_2026-08-27"


def test_every_producer_tags_its_events(tmp_path: Path, monkeypatch):
    from src import swim_file_ingest
    from src.demo_scenario import generate_cascade_failure_scenario
    from src.replay import replay_from_directory

    assert {e.data_origin for e in generate_cascade_failure_scenario()} == {"synthetic"}

    monkeypatch.setattr(swim_file_ingest, "_get_already_processed", lambda names: set())
    monkeypatch.setattr(swim_file_ingest, "_append_log_entries", lambda entries: None)
    folder = tmp_path / "log"
    folder.mkdir()
    shutil.copy(CAPTURE / "1787863346242", folder / "1787863346242")
    live, _ = swim_file_ingest.scan_swim_folder(folder, processed_dir=tmp_path / "p", failed_dir=tmp_path / "f")
    assert live and {e.data_origin for e in live} == {"live"}

    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "x.json").write_text(json.dumps({
        "airport_code": "KDCA", "tracon": "PCT", "service_name": "ISMC", "message_type": "m",
        "event_time_utc": "2026-09-25T12:00:00+00:00", "received_time_utc": "2026-09-25T12:00:01+00:00",
        "feed_status": "operational"}))
    replayed, _ = replay_from_directory(tmp_path / "r")
    assert {e.data_origin for e in replayed} == {"replay"}


def test_a_file_cannot_declare_itself_live(tmp_path: Path, monkeypatch):
    """Provenance is set by the scanner, not read from the message."""
    from src.swim_file_ingest import _normalise_record

    event, _ = _normalise_record({"airport_code": "KDCA", "tracon": "PCT", "service_name": "ISMC",
                                  "message_type": "m", "event_time_utc": "2026-09-25T12:00:00Z",
                                  "data_origin": "live"}, "x.json", index=0, data_origin="replay")
    assert event.data_origin == "replay"


def _event(origin, service="ISMC", feed_status="disconnected", minutes_ago=0.5):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return NASEvent(airport_code="KDCA", tracon="PCT", service_name=service, message_type="m",
                    event_time_utc=t, received_time_utc=t, latency_ms=100.0,
                    feed_status=feed_status, data_origin=origin)


def test_alerts_inherit_their_events_origin_and_aggregates_say_mixed():
    per_event = check_alerts([_event("synthetic")])
    assert per_event and {a.data_origin for a in per_event} == {"synthetic"}

    # An aggregate alert (a surface-event spike) over live + synthetic events is "mixed".
    now = datetime.now(timezone.utc)
    spike = [NASEvent(airport_code="KDCA", tracon="PCT", service_name="SMES", message_type="SURFACE_MOVEMENT",
                      event_time_utc=now, received_time_utc=now, latency_ms=100.0, track_id="TRK1234",
                      surface_event_type="TAXI_OUT", data_origin="live" if i % 2 else "synthetic")
             for i in range(settings.SURFACE_SPIKE_COUNT)]
    agg = [a for a in check_alerts(spike) if a.alert_type == "ALERT_SURFACE_EVENT_SPIKE"]
    assert agg and agg[0].data_origin == "mixed"


def test_api_clients_cannot_label_their_events_live(monkeypatch):
    import src.api as api_module

    captured = []

    async def fake_pipeline(events, **kw):
        captured.extend(events)
        return "wf"

    monkeypatch.setattr(api_module, "start_pipeline_workflow", fake_pipeline)
    monkeypatch.setattr(settings, "API_KEY", "k")
    monkeypatch.setattr(settings, "API_KEYS", {})
    payload = {"airport_code": "KDCA", "tracon": "PCT", "service_name": "ISMC", "message_type": "m",
               "event_time_utc": "2026-09-25T12:00:00Z", "received_time_utc": "2026-09-25T12:00:01Z",
               "feed_status": "operational", "data_origin": "live"}
    resp = TestClient(api_module.app).post("/v1/ingest", json={"events": [payload]}, headers={"X-API-Key": "k"})
    assert resp.status_code == 200
    assert [e["data_origin"] for e in captured] == ["api"]


def test_the_critic_flags_an_alert_that_did_not_come_from_the_live_feed():
    from src.investigation.record import Investigation
    from src.investigation.specialists import RedTeamCritic
    from src.investigation.evidence import Snapshot, collect
    from src.models import AlertEvent

    alert = AlertEvent(alert_type="ALERT_FEED_DISCONNECTED", severity="CRITICAL", airport_code="KDCA",
                       service_name="ISMC", description="d", data_origin="synthetic")
    snap = Snapshot(taken_at=datetime.now(timezone.utc), alert=alert, action_id="A", events=[],
                    alerts=[], ingest_log=[], failed_files=[], rvr=[], staged_actions=[])
    inv = Investigation(action_id="A", alert_id=alert.alert_id, trigger={}, evidence=collect(snap))
    challenges = RedTeamCritic().rule_challenges(inv)
    assert any("synthetic data, not the live FAA feed" in c.statement for c in challenges)


# ── Round trip through the database ──────────────────────────────────────────

@pytest.fixture
def db():
    storage.override_db_path(":memory:")
    yield storage
    storage.override_db_path(":memory:")


async def test_origin_survives_storage_and_is_counted_for_the_banner(db):
    events = [_event("synthetic"), _event("live", service="SMES", feed_status="operational")]
    events[1] = events[1].model_copy(update={"track_id": "1"})
    await db.save_events(events)
    await db.save_alerts(check_alerts(events))

    rows = {r["event_id"]: r["data_origin"] for r in await db.get_recent_events(limit=10)}
    assert sorted(rows.values()) == ["live", "synthetic"]
    alerts = await db.get_recent_alerts(limit=10)
    assert any(a["data_origin"] == "synthetic" for a in alerts)
    assert await db.get_origin_counts(window_minutes=60) == {"live": 1, "synthetic": 1}
