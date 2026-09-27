"""Tests for src/alert_engine.py — all seven alert rule types."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import config
from src.alert_engine import check_alerts, stale_feed_alerts
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

def test_a_feed_silent_past_the_limit_raises_a_stale_alert():
    now = _now()
    silent = now - timedelta(seconds=config.STALE_FEED_SECONDS + 120)
    [alert] = stale_feed_alerts({("KIAD", "SMES"): silent, ("KDCA", "SMES"): now}, now)
    assert (alert.alert_type, alert.severity, alert.airport_code, alert.service_name) == \
        ("ALERT_FEED_STALE", "WARNING", "KIAD", "SMES")
    assert alert.data_origin == "live"


def test_a_fresh_feed_is_not_stale():
    assert stale_feed_alerts({("KDCA", "SMES"): _now()}) == []


def _start_of_an_evaluation_window() -> tuple[datetime, int]:
    """A moment pinned to the start of a window, so "a minute later" cannot fall in the next one."""
    window = max(config.SURFACE_SPIKE_WINDOW_SECONDS, config.STALE_FEED_SECONDS)
    assert window > 60
    return datetime.fromtimestamp((int(_now().timestamp() // window) + 1) * window, tz=timezone.utc), window


def test_the_same_silence_re_evaluated_is_the_same_alert():
    now, _ = _start_of_an_evaluation_window()
    silent = {("KIAD", "SMES"): now - timedelta(seconds=config.STALE_FEED_SECONDS + 120)}
    [first] = stale_feed_alerts(silent, now)
    [again] = stale_feed_alerts(silent, now + timedelta(seconds=60))
    assert first.alert_id == again.alert_id, "one alert per evaluation window, not one per reconciler pass"


def test_the_same_silence_in_the_next_window_is_a_new_alert():
    now, window = _start_of_an_evaluation_window()
    silent = {("KIAD", "SMES"): now - timedelta(seconds=config.STALE_FEED_SECONDS + 120)}
    [first] = stale_feed_alerts(silent, now)
    [later] = stale_feed_alerts(silent, now + timedelta(seconds=window))
    assert first.alert_id != later.alert_id, "a silence that goes on is put in front of a person again"


def test_a_batch_of_old_messages_is_not_a_stale_feed():
    """A silent feed sends no batch, and a backlog of old files is ingest lag, not silence."""
    old = NASEvent(
        airport_code="KIAD", tracon="PCT", service_name="SMES", message_type="SURFACE_MOVEMENT",
        event_time_utc=_now() - timedelta(seconds=config.STALE_FEED_SECONDS + 121),
        received_time_utc=_now() - timedelta(seconds=config.STALE_FEED_SECONDS + 120),
        latency_ms=1_000.0, track_id="TRK9999", validation_status="PASSED",
    )
    assert "ALERT_FEED_STALE" not in _alert_types([old])
