"""Tests for src/replay.py — message loading, parsing, and round-trip integrity."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from src.replay import (
    load_raw_messages,
    parse_raw_message,
    replay_from_directory,
    write_sample_replay_file,
)
from src.sample_data import generate_sample_batch


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


# ── load_raw_messages ─────────────────────────────────────────────────────────

def test_replay_loads_single_object_file(tmp_path):
    now = datetime.utcnow()
    msg = {
        "airport_code": "KDCA",
        "tracon": "PCT",
        "service_name": "SMES",
        "message_type": "SURFACE_MOVEMENT",
        "event_time_utc": _iso(now - timedelta(seconds=2)),
        "received_time_utc": _iso(now),
        "latency_ms": 2_000.0,
        "track_id": "TRK0001",
    }
    _write_json(tmp_path / "single.json", msg)
    raw = load_raw_messages(tmp_path)
    assert len(raw) == 1
    assert raw[0]["airport_code"] == "KDCA"


def test_replay_loads_list_file(tmp_path):
    now = datetime.utcnow()
    msgs = [
        {
            "airport_code": "KIAD",
            "tracon": "PCT",
            "service_name": "APDS_RVR",
            "message_type": "RVR_UPDATE",
            "event_time_utc": _iso(now - timedelta(seconds=1)),
            "received_time_utc": _iso(now),
            "latency_ms": 500.0,
            "runway": "19L",
            "rvr_touchdown": 2_400,
        },
        {
            "airport_code": "KBWI",
            "tracon": "PCT",
            "service_name": "SMES",
            "message_type": "SURFACE_MOVEMENT",
            "event_time_utc": _iso(now - timedelta(seconds=3)),
            "received_time_utc": _iso(now),
            "latency_ms": 3_000.0,
            "track_id": "TRK9999",
        },
    ]
    _write_json(tmp_path / "batch.json", msgs)
    raw = load_raw_messages(tmp_path)
    assert len(raw) == 2


def test_replay_skips_invalid_json(tmp_path):
    (tmp_path / "bad.json").write_text("NOT_JSON{{{", encoding="utf-8")
    raw = load_raw_messages(tmp_path)
    assert raw == []


def test_replay_aggregates_multiple_files(tmp_path):
    now = datetime.utcnow()
    base = dict(
        airport_code="KDCA", tracon="PCT", service_name="SMES",
        message_type="SURFACE_MOVEMENT",
        event_time_utc=_iso(now - timedelta(seconds=1)),
        received_time_utc=_iso(now), latency_ms=1_000.0, track_id="TRK1",
    )
    _write_json(tmp_path / "a.json", [base])
    _write_json(tmp_path / "b.json", [{**base, "track_id": "TRK2"}])
    raw = load_raw_messages(tmp_path)
    assert len(raw) == 2


# ── parse_raw_message ─────────────────────────────────────────────────────────

def test_parse_valid_message():
    now = datetime.utcnow()
    raw = {
        "airport_code": "KIAD",
        "tracon": "PCT",
        "service_name": "APDS_RVR",
        "message_type": "RVR_UPDATE",
        "event_time_utc": _iso(now - timedelta(seconds=1)),
        "received_time_utc": _iso(now),
        "runway": "19L",
        "rvr_touchdown": 2_400,
    }
    event, error = parse_raw_message(raw)
    assert event is not None
    assert error is None
    assert event.airport_code == "KIAD"
    assert event.runway == "19L"
    assert event.latency_ms is not None and event.latency_ms > 0


def test_parse_invalid_message_returns_error():
    raw = {"not_a_valid_field": True}
    event, error = parse_raw_message(raw)
    assert event is None
    assert error is not None


def test_parse_recalculates_latency():
    now = datetime.utcnow()
    raw = {
        "airport_code": "KDCA",
        "tracon": "PCT",
        "service_name": "SMES",
        "message_type": "SURFACE_MOVEMENT",
        "event_time_utc": _iso(now - timedelta(seconds=5)),
        "received_time_utc": _iso(now),
        "latency_ms": 999.0,   # stale value — should be recalculated
        "track_id": "TRK1",
    }
    event, _ = parse_raw_message(raw)
    assert event is not None
    # Should be recalculated to approximately 5000 ms
    assert event.latency_ms is not None
    assert abs(event.latency_ms - 5_000) < 100


# ── replay_from_directory ─────────────────────────────────────────────────────

def test_replay_from_directory_returns_events(tmp_path):
    now = datetime.utcnow()
    msgs = [{
        "airport_code": "KADW",
        "tracon": "PCT",
        "service_name": "TOWER_DEPARTURE",
        "message_type": "DEPARTURE_EVENT",
        "event_time_utc": _iso(now - timedelta(seconds=2)),
        "received_time_utc": _iso(now),
        "runway": "01L",
        "aircraft_id": "AAL789",
        "track_id": "TRK0042",
        "surface_event_type": "TAKEOFF",
    }]
    _write_json(tmp_path / "deps.json", msgs)
    events, errors = replay_from_directory(tmp_path)
    assert len(events) == 1
    assert errors == []
    assert events[0].airport_code == "KADW"


def test_replay_handles_mixed_valid_invalid(tmp_path):
    now = datetime.utcnow()
    msgs = [
        {
            "airport_code": "KDCA",
            "tracon": "PCT",
            "service_name": "SMES",
            "message_type": "SURFACE_MOVEMENT",
            "event_time_utc": _iso(now - timedelta(seconds=1)),
            "received_time_utc": _iso(now),
            "track_id": "TRK1",
        },
        {"totally": "invalid"},
    ]
    _write_json(tmp_path / "mix.json", msgs)
    events, errors = replay_from_directory(tmp_path)
    assert len(events) == 1
    assert len(errors) == 1


# ── write_sample_replay_file ──────────────────────────────────────────────────

def test_write_and_reload_roundtrip(tmp_path):
    sample_events = generate_sample_batch(20)
    output = write_sample_replay_file(sample_events, tmp_path / "sample.json")
    assert output.exists()

    events, errors = replay_from_directory(tmp_path)
    assert len(events) == 20
    assert len(errors) == 0
    # Field integrity check
    airports = {e.airport_code for e in events}
    assert airports.issubset({"KDCA", "KIAD", "KBWI", "KADW"})
