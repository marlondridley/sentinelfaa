"""Tests for src/feed_health.py — health aggregation and stale feed detection."""
from __future__ import annotations

from datetime import datetime, timedelta

from src import config
from src.feed_health import compute_feed_health
from src.models import NASEvent


def _now():
    return datetime.utcnow()


def _event(
    service="SMES",
    airport="KDCA",
    age_seconds=10,
    feed_status=None,
    latency_ms=1_000.0,
) -> NASEvent:
    """Build a NASEvent with a configurable received_time age."""
    received = _now() - timedelta(seconds=age_seconds)
    event_t = received - timedelta(milliseconds=latency_ms)
    return NASEvent(
        airport_code=airport,
        tracon="PCT",
        service_name=service,
        message_type="TEST",
        event_time_utc=event_t,
        received_time_utc=received,
        latency_ms=latency_ms,
        track_id="TRK001",
        feed_status=feed_status,
        validation_status="PASSED",
    )


# ── Status classification ─────────────────────────────────────────────────────

def test_fresh_feed_is_ok():
    health = compute_feed_health([_event(age_seconds=30)])
    assert health["airport_status"].get("KDCA") == "OK"


def test_stale_feed_detected():
    stale_age = config.STALE_FEED_SECONDS + 60
    health = compute_feed_health([_event(age_seconds=stale_age)])
    assert health["airport_status"].get("KDCA") == "STALE"


def test_just_under_stale_threshold_is_ok():
    fresh_age = config.STALE_FEED_SECONDS - 10
    health = compute_feed_health([_event(age_seconds=fresh_age)])
    assert health["airport_status"].get("KDCA") == "OK"


def test_degraded_feed_status_reported():
    health = compute_feed_health([_event(service="ISMC", feed_status="degraded")])
    assert health["airport_status"].get("KDCA") == "DEGRADED"


def test_disconnected_feed_status_worst():
    events = [
        _event(feed_status="degraded"),
        _event(feed_status="disconnected"),
        _event(feed_status="operational"),
    ]
    health = compute_feed_health(events)
    # DISCONNECTED should win over DEGRADED and OK
    assert health["airport_status"].get("KDCA") == "DISCONNECTED"


def test_unavailable_beats_degraded():
    events = [
        _event(feed_status="degraded"),
        _event(feed_status="unavailable"),
    ]
    health = compute_feed_health(events)
    assert health["airport_status"].get("KDCA") == "UNAVAILABLE"


# ── SMES counts ───────────────────────────────────────────────────────────────

def test_smes_count_tracked_per_airport():
    events = [_event(service="SMES", airport="KBWI") for _ in range(7)]
    health = compute_feed_health(events)
    assert health["smes_counts"].get("KBWI") == 7


def test_smes_counts_isolated_by_airport():
    events = (
        [_event(service="SMES", airport="KDCA") for _ in range(3)] +
        [_event(service="SMES", airport="KIAD") for _ in range(5)]
    )
    health = compute_feed_health(events)
    assert health["smes_counts"]["KDCA"] == 3
    assert health["smes_counts"]["KIAD"] == 5


def test_non_smes_events_not_counted():
    events = [_event(service="ISMC", airport="KDCA", feed_status="operational")]
    health = compute_feed_health(events)
    assert health["smes_counts"].get("KDCA", 0) == 0


# ── Latency statistics ────────────────────────────────────────────────────────

def test_latency_stats_computed():
    events = [
        _event(service="SMES", latency_ms=1_000.0),
        _event(service="SMES", latency_ms=3_000.0),
    ]
    health = compute_feed_health(events)
    stats = health["latency_stats"]["SMES"]
    assert stats["avg_ms"] == 2_000.0
    assert stats["min_ms"] == 1_000.0
    assert stats["max_ms"] == 3_000.0
    assert stats["count"] == 2


def test_latency_stats_per_service():
    events = [
        _event(service="SMES", latency_ms=500.0),
        _event(service="APDS_RVR", airport="KIAD", latency_ms=800.0),
    ]
    health = compute_feed_health(events)
    assert "SMES" in health["latency_stats"]
    assert "APDS_RVR" in health["latency_stats"]
    assert health["latency_stats"]["SMES"]["avg_ms"] == 500.0
    assert health["latency_stats"]["APDS_RVR"]["avg_ms"] == 800.0


# ── Service-level status ──────────────────────────────────────────────────────

def test_service_status_keyed_by_service_and_airport():
    events = [
        _event(service="SMES", airport="KDCA"),
        _event(service="ISMC", airport="KBWI", feed_status="operational"),
    ]
    health = compute_feed_health(events)
    assert "SMES" in health["service_status"]
    assert "KDCA" in health["service_status"]["SMES"]
    assert "ISMC" in health["service_status"]
    assert "KBWI" in health["service_status"]["ISMC"]


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_empty_event_list():
    health = compute_feed_health([])
    assert health["airport_status"] == {}
    assert health["total_events"] == 0


def test_total_events_counted():
    events = [_event() for _ in range(42)]
    health = compute_feed_health(events)
    assert health["total_events"] == 42
