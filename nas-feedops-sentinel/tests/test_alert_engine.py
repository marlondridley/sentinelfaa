"""Tests for src/alert_engine.py — all seven alert rule types."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import config
from src.alert_engine import check_alerts
from src.models import NASEvent


def _now():
    return datetime.now(timezone.utc)


def _smes(airport="KDCA", latency_ms=1_000.0, feed_status=None,
          validation_status="PASSED", validation_errors=None) -> NASEvent:
    return NASEvent(
        airport_code=airport,
        tracon="PCT",
        service_name="SMES",
        message_type="SURFACE_MOVEMENT",
        event_time_utc=_now() - timedelta(milliseconds=latency_ms),
        received_time_utc=_now(),
        latency_ms=latency_ms,
        track_id="TRK1234",
        surface_event_type="TAXI_OUT",
        feed_status=feed_status,
        validation_status=validation_status,
        validation_errors=validation_errors or [],
    )


def _rvr(airport="KDCA", runway="01", touchdown=2_400,
         midpoint=None, rollout=None) -> NASEvent:
    return NASEvent(
        airport_code=airport,
        tracon="PCT",
        service_name="APDS_RVR",
        message_type="RVR_UPDATE",
        event_time_utc=_now() - timedelta(seconds=1),
        received_time_utc=_now(),
        latency_ms=500.0,
        runway=runway,
        rvr_touchdown=touchdown,
        rvr_midpoint=midpoint,
        rvr_rollout=rollout,
        validation_status="PASSED",
    )


def _ismc(airport="KDCA", feed_status="operational") -> NASEvent:
    return NASEvent(
        airport_code=airport,
        tracon="PCT",
        service_name="ISMC",
        message_type="LINK_STATUS",
        event_time_utc=_now() - timedelta(seconds=1),
        received_time_utc=_now(),
        latency_ms=300.0,
        feed_status=feed_status,
        validation_status="PASSED",
    )


def _alert_types(events):
    return {a.alert_type for a in check_alerts(events)}


# ── ALERT_LOW_RVR ─────────────────────────────────────────────────────────────

def test_low_rvr_triggers_alert():
    assert "ALERT_LOW_RVR" in _alert_types([_rvr(touchdown=800)])


def test_rvr_at_threshold_does_not_trigger():
    assert "ALERT_LOW_RVR" not in _alert_types([_rvr(touchdown=config.RVR_LOW_THRESHOLD_FT)])


def test_low_rvr_midpoint_triggers_alert():
    assert "ALERT_LOW_RVR" in _alert_types([_rvr(touchdown=3_000, midpoint=900)])


def test_normal_rvr_no_alert():
    assert "ALERT_LOW_RVR" not in _alert_types([_rvr(touchdown=3_000)])


# ── ALERT_HIGH_LATENCY ────────────────────────────────────────────────────────

def test_high_latency_triggers_alert():
    assert "ALERT_HIGH_LATENCY" in _alert_types([_smes(latency_ms=35_000)])


def test_latency_just_at_threshold_no_alert():
    assert "ALERT_HIGH_LATENCY" not in _alert_types(
        [_smes(latency_ms=config.LATENCY_ALERT_MS)]
    )


def test_normal_latency_no_alert():
    assert "ALERT_HIGH_LATENCY" not in _alert_types([_smes(latency_ms=500)])


# ── ALERT_ISMC_LINK_DEGRADED ──────────────────────────────────────────────────

def test_ismc_degraded_triggers_alert():
    assert "ALERT_ISMC_LINK_DEGRADED" in _alert_types([_ismc(feed_status="degraded")])


def test_ismc_unavailable_triggers_alert():
    assert "ALERT_ISMC_LINK_DEGRADED" in _alert_types([_ismc(feed_status="unavailable")])


def test_ismc_operational_no_alert():
    assert "ALERT_ISMC_LINK_DEGRADED" not in _alert_types([_ismc(feed_status="operational")])


# ── ALERT_FEED_DISCONNECTED ───────────────────────────────────────────────────

def test_feed_disconnected_triggers_alert():
    assert "ALERT_FEED_DISCONNECTED" in _alert_types([_smes(feed_status="disconnected")])


def test_feed_operational_no_disconnected_alert():
    assert "ALERT_FEED_DISCONNECTED" not in _alert_types([_smes(feed_status="operational")])


# ── ALERT_SCHEMA_VALIDATION_FAILED ───────────────────────────────────────────

def test_validation_failed_triggers_alert():
    event = _smes(
        validation_status="FAILED",
        validation_errors=["SMES message must include aircraft_id, vehicle_id, or track_id."],
    )
    assert "ALERT_SCHEMA_VALIDATION_FAILED" in _alert_types([event])


def test_validation_passed_no_alert():
    assert "ALERT_SCHEMA_VALIDATION_FAILED" not in _alert_types([_smes()])


# ── ALERT_SURFACE_EVENT_SPIKE ─────────────────────────────────────────────────

def test_surface_event_spike_triggers_alert():
    spike = config.SURFACE_SPIKE_COUNT + 5
    events = [_smes(airport="KDCA") for _ in range(spike)]
    assert "ALERT_SURFACE_EVENT_SPIKE" in _alert_types(events)


def test_below_spike_threshold_no_alert():
    events = [_smes(airport="KDCA") for _ in range(config.SURFACE_SPIKE_COUNT - 1)]
    assert "ALERT_SURFACE_EVENT_SPIKE" not in _alert_types(events)


# ── ALERT_FEED_STALE ──────────────────────────────────────────────────────────

def test_stale_feed_triggers_alert():
    stale_age = config.STALE_FEED_SECONDS + 120
    event = NASEvent(
        airport_code="KIAD",
        tracon="PCT",
        service_name="SMES",
        message_type="SURFACE_MOVEMENT",
        event_time_utc=_now() - timedelta(seconds=stale_age + 1),
        received_time_utc=_now() - timedelta(seconds=stale_age),
        latency_ms=1_000.0,
        track_id="TRK9999",
        validation_status="PASSED",
    )
    assert "ALERT_FEED_STALE" in _alert_types([event])


def test_fresh_feed_no_stale_alert():
    assert "ALERT_FEED_STALE" not in _alert_types([_smes()])
