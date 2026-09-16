"""Tests for src/validators.py — schema and business-rule validation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.models import NASEvent
from src.validators import validate_event


def _now():
    return datetime.now(timezone.utc)


def _base(**overrides) -> NASEvent:
    """A minimal valid SMES event."""
    defaults = dict(
        airport_code="KDCA",
        tracon="PCT",
        service_name="SMES",
        message_type="SURFACE_MOVEMENT",
        event_time_utc=_now() - timedelta(seconds=1),
        received_time_utc=_now(),
        latency_ms=1_000.0,
        track_id="TRK1234",
    )
    defaults.update(overrides)
    return NASEvent(**defaults)


# ── RVR tests ─────────────────────────────────────────────────────────────────

def test_valid_rvr_message_passes():
    event = NASEvent(
        airport_code="KIAD",
        tracon="PCT",
        service_name="APDS_RVR",
        message_type="RVR_UPDATE",
        event_time_utc=_now() - timedelta(seconds=1),
        received_time_utc=_now(),
        latency_ms=500.0,
        runway="19L",
        rvr_touchdown=2_400,
    )
    result = validate_event(event)
    assert result.passed, f"Expected pass, got: {result.errors}"


def test_rvr_missing_runway_fails():
    event = NASEvent(
        airport_code="KBWI",
        tracon="PCT",
        service_name="APDS_RVR",
        message_type="RVR_UPDATE",
        event_time_utc=_now() - timedelta(seconds=1),
        received_time_utc=_now(),
        latency_ms=200.0,
        rvr_touchdown=1_800,
        # runway intentionally omitted
    )
    result = validate_event(event)
    assert not result.passed
    assert any("runway" in e.lower() for e in result.errors)


def test_rvr_missing_all_values_fails():
    event = NASEvent(
        airport_code="KBWI",
        tracon="PCT",
        service_name="APDS_RVR",
        message_type="RVR_UPDATE",
        event_time_utc=_now() - timedelta(seconds=1),
        received_time_utc=_now(),
        latency_ms=200.0,
        runway="10",
        # all rvr values omitted
    )
    result = validate_event(event)
    assert not result.passed
    assert any("rvr value" in e.lower() for e in result.errors)


# ── Timestamp tests ───────────────────────────────────────────────────────────

def test_missing_event_timestamp_raises_on_construction():
    """Pydantic should reject None for a required datetime field."""
    with pytest.raises(Exception):
        NASEvent(
            airport_code="KDCA",
            tracon="PCT",
            service_name="SMES",
            message_type="SURFACE_MOVEMENT",
            event_time_utc=None,  # type: ignore[arg-type]
            received_time_utc=_now(),
            latency_ms=100.0,
            track_id="TRK1",
        )


def test_missing_latency_fails_validation():
    event = _base(latency_ms=None)
    result = validate_event(event)
    assert not result.passed
    assert any("latency_ms" in e for e in result.errors)


# ── Airport / TRACON tests ────────────────────────────────────────────────────

def test_invalid_airport_code_fails():
    with pytest.raises(Exception):
        _base(airport_code="KJFK")


def test_invalid_tracon_fails():
    with pytest.raises(Exception):
        _base(tracon="N90")


def test_invalid_service_name_fails():
    with pytest.raises(Exception):
        _base(service_name="UNKNOWN_SVC")


# ── SMES entity tests ─────────────────────────────────────────────────────────

def test_smes_without_track_aircraft_vehicle_fails():
    event = _base(service_name="SMES", track_id=None, aircraft_id=None, vehicle_id=None)
    result = validate_event(event)
    assert not result.passed
    assert any("SMES" in e for e in result.errors)


def test_smes_with_aircraft_id_passes():
    event = _base(service_name="SMES", aircraft_id="UAL123", track_id=None)
    result = validate_event(event)
    assert result.passed


def test_smes_with_vehicle_id_passes():
    event = _base(service_name="SMES", vehicle_id="TUG-01", track_id=None)
    result = validate_event(event)
    assert result.passed


# ── ISMC tests ────────────────────────────────────────────────────────────────

def test_ismc_without_feed_status_fails():
    event = NASEvent(
        airport_code="KDCA",
        tracon="PCT",
        service_name="ISMC",
        message_type="LINK_STATUS",
        event_time_utc=_now() - timedelta(seconds=1),
        received_time_utc=_now(),
        latency_ms=300.0,
        # feed_status omitted
    )
    result = validate_event(event)
    assert not result.passed
    assert any("ISMC" in e for e in result.errors)


def test_ismc_with_feed_status_passes():
    event = NASEvent(
        airport_code="KADW",
        tracon="PCT",
        service_name="ISMC",
        message_type="LINK_STATUS",
        event_time_utc=_now() - timedelta(seconds=1),
        received_time_utc=_now(),
        latency_ms=300.0,
        feed_status="operational",
    )
    result = validate_event(event)
    assert result.passed
