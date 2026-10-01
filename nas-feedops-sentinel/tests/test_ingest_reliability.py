"""
Ingest reliability (results.md R-03, R-04, R-05, R-06).

R-03  no index led with received_time_utc; aggregates with no window scanned all history
R-04  a claim restamped the file, so released files looked newer than they were and waited out a 10-minute timeout
R-05  one out-of-range number or NUL in one message rejected a batch of up to 500 files, for ever
R-06  the alert engine looked each alert's origin up with a scan of every event
"""
from __future__ import annotations

import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest

from src import storage, swim_file_ingest
from src.alert_engine import check_alerts
from src.models import NASEvent
from tests.test_swim_kit_ingest import CAPTURE, _isolate_ingest_log

_NAME = "1787863348274"


@pytest.fixture(autouse=True)
def isolated_db():
    storage.override_db_path(":memory:")
    yield
    storage.override_db_path(":memory:")


def _event(**overrides) -> NASEvent:
    now = datetime.now(timezone.utc)
    fields = dict(airport_code="KDCA", tracon="PCT", service_name="APDS_RVR", message_type="m",
                  event_time_utc=now, received_time_utc=now, latency_ms=100.0, runway="01", rvr_touchdown=2400,
                  feed_status="operational", validation_status="PASSED", data_origin="live")
    fields.update(overrides)
    return NASEvent(**fields)


# ── R-03 ───────────────────────────────────────────────────────────────────────

async def test_an_index_leads_with_the_receipt_time_and_the_newest_n_query_uses_it():
    from src.storage.pool import get_db_pool

    await storage.save_events([_event(event_id="e1")])
    pool = await get_db_pool()
    assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE id = '2026_10_nas_events_received_time_index'") == 1
    async with pool.acquire() as conn:
        await conn.execute("SET enable_seqscan = off")       # a one-row table would otherwise never use an index
        plan = "\n".join(r[0] for r in await conn.fetch(
            "EXPLAIN SELECT event_id FROM nas_events ORDER BY received_time_utc DESC LIMIT 10"))
    assert "idx_nas_events_received_time" in plan, plan
    assert "Sort" not in plan, "the index gives the order; nothing sorts the table"


async def test_an_aggregate_with_no_window_reads_only_the_recent_window(monkeypatch):
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=30)
    await storage.save_events([
        _event(event_id="old", received_time_utc=old, event_time_utc=old, latency_ms=90_000.0, validation_status="FAILED"),
        _event(event_id="new", latency_ms=1_000.0),
    ])
    summary = await storage.get_validation_summary()
    assert (summary["total"], summary["passed"], summary["failed"]) == (1, 1, 0)
    stats = await storage.get_latency_stats()
    assert stats["max_ms"] == 1_000.0, "a month-old 90 s outlier is not today's latency"

    everything = await storage.get_validation_summary(since=old - timedelta(days=1))
    assert everything["total"] == 2, "an explicit window still reaches back"


async def test_the_latest_rvr_ignores_a_runway_that_has_not_reported_in_the_window():
    now = datetime.now(timezone.utc)
    stale = now - timedelta(days=3)
    await storage.save_events([
        _event(event_id="r1", runway="01", rvr_touchdown=1800),
        _event(event_id="r2", runway="19", received_time_utc=stale, event_time_utc=stale, rvr_touchdown=600),
    ])
    assert [(r["runway"], r["rvr_touchdown"]) for r in await storage.get_latest_rvr()] == [("01", 1800)]
    assert len(await storage.get_latest_rvr(since=stale - timedelta(hours=1))) == 2


# ── R-04 ───────────────────────────────────────────────────────────────────────

def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / "log"
    folder.mkdir()
    shutil.copy(CAPTURE / _NAME, folder / _NAME)
    return folder


def _age(path: Path, seconds: float) -> float:
    when = time.time() - seconds
    os.utime(path, (when, when))
    return when


def test_a_claim_leaves_the_files_own_timestamps_alone(tmp_path, monkeypatch):
    _isolate_ingest_log(monkeypatch)
    folder = _folder(tmp_path)
    arrived = _age(folder / _NAME, 90)

    swim_file_ingest.scan_swim_folder(folder, processed_dir=tmp_path / "p", failed_dir=tmp_path / "f", defer_archive=True)

    claimed = folder / swim_file_ingest.INFLIGHT_DIR / _NAME
    assert claimed.stat().st_mtime == pytest.approx(arrived, abs=1), "the arrival time, not the claim time"
    assert (folder / swim_file_ingest.INFLIGHT_DIR / f"{_NAME}.claim").exists(), "the claim's age is kept apart"


def test_a_failed_pipeline_gives_the_files_back_at_once_with_their_own_time(tmp_path, monkeypatch):
    _isolate_ingest_log(monkeypatch)
    folder = _folder(tmp_path)
    arrived = _age(folder / _NAME, 90)
    _, stats = swim_file_ingest.scan_swim_folder(folder, processed_dir=tmp_path / "p", failed_dir=tmp_path / "f",
                                                 defer_archive=True)

    assert swim_file_ingest.release_scan(stats) == 1

    assert (folder / _NAME).exists() and (folder / _NAME).stat().st_mtime == pytest.approx(arrived, abs=1)
    assert not list((folder / swim_file_ingest.INFLIGHT_DIR).iterdir()), "no file and no marker left behind"
    events, stats = swim_file_ingest.scan_swim_folder(folder, processed_dir=tmp_path / "p", failed_dir=tmp_path / "f",
                                                      defer_archive=True)
    assert len(events) == 1, "scanned again straight away, not after a timeout"
    assert events[0].received_time_utc.timestamp() == pytest.approx(arrived, abs=2), "receipt time is the arrival, not a re-stamp"


def test_a_dead_scanners_claim_is_released_by_the_claims_age_not_the_files(tmp_path, monkeypatch):
    _isolate_ingest_log(monkeypatch)
    folder = _folder(tmp_path)
    arrived = _age(folder / _NAME, 3600)        # an hour-old message, claimed just now
    swim_file_ingest.scan_swim_folder(folder, processed_dir=tmp_path / "p", failed_dir=tmp_path / "f", defer_archive=True)

    assert swim_file_ingest._release_stale_claims(folder) == 0, "claimed a moment ago: still the scanner's"
    marker = folder / swim_file_ingest.INFLIGHT_DIR / f"{_NAME}.claim"
    _age(marker, swim_file_ingest.CLAIM_TIMEOUT_SECONDS + 5)
    assert swim_file_ingest._release_stale_claims(folder) == 1
    assert (folder / _NAME).stat().st_mtime == pytest.approx(arrived, abs=1)
    assert not marker.exists()


def test_the_claim_timeout_is_shorter_than_the_janitors_age():
    """A file released after a crash must be scanned again before the janitor (5 min) can drop it."""
    assert swim_file_ingest.CLAIM_TIMEOUT_SECONDS < 5 * 60
    script = (Path(__file__).resolve().parent.parent / "scripts" / "Cleanup-KitLog.ps1").read_text(encoding="utf-8")
    assert "[int]$MaxAgeMinutes = 5" in script


def test_committing_or_quarantining_removes_the_marker(tmp_path, monkeypatch):
    _isolate_ingest_log(monkeypatch)
    folder = _folder(tmp_path)
    _, stats = swim_file_ingest.scan_swim_folder(folder, processed_dir=tmp_path / "p", failed_dir=tmp_path / "f",
                                                 defer_archive=True)
    swim_file_ingest.commit_scan(stats)
    assert not list((folder / swim_file_ingest.INFLIGHT_DIR).iterdir())


async def test_the_temporal_activity_gives_the_files_back_when_the_pipeline_fails(tmp_path, monkeypatch):
    from src import config, temporal_activities

    _isolate_ingest_log(monkeypatch)
    raw = tmp_path / "raw" / "swim_live"
    raw.mkdir(parents=True)
    shutil.copy(CAPTURE / _NAME, raw / _NAME)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(swim_file_ingest, "PROCESSED_DIR", tmp_path / "p")
    monkeypatch.setattr(swim_file_ingest, "FAILED_DIR", tmp_path / "f")

    async def down(events, **kw):
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(temporal_activities, "run_pipeline", down)
    with pytest.raises(ConnectionError):
        await temporal_activities.ingest_folder_activity(str(raw), "cid-1")

    assert (raw / _NAME).exists(), "visible to the retry Temporal will make, which used to find an empty folder"


# ── R-05 ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("field", ["altitude_ft", "ground_speed_kt", "vertical_speed_fpm", "rvr_touchdown"])
@pytest.mark.parametrize("value", [2_147_483_648, -2_147_483_649, 3_000_000_000])
def test_a_number_outside_32_bits_is_refused_by_the_model_not_the_database(field, value):
    with pytest.raises(ValueError):
        _event(**{field: value})
    assert getattr(_event(**{field: 2_147_483_647}), field) == 2_147_483_647


def test_a_nul_is_removed_from_every_text_field():
    event = _event(runway="0\x001", aircraft_id="AA\x00L1", validation_errors=["bad\x00thing"])
    assert (event.runway, event.aircraft_id, event.validation_errors) == ("01", "AAL1", ["badthing"])


def test_the_record_with_a_huge_value_fails_alone_and_its_siblings_do_not():
    from src.swim_normalise import _normalise_record

    base = {"airport": "KDCA", "tracon": "PCT", "service": "APDS_RVR", "message_type": "m"}
    event, err = _normalise_record({**base, "altitude_ft": 3_000_000_000}, "ref", index=0, data_origin="live",
                                   received_at=datetime.now(timezone.utc))
    assert event is None and err


async def test_a_nul_and_a_huge_number_no_longer_reach_the_database():
    await storage.save_events([_event(event_id="ok", runway="0\x001", altitude_ft=2_000_000_000)])
    rows = await storage.get_recent_events(limit=10)
    assert [(r["runway"], r["altitude_ft"]) for r in rows] == [("01", 2_000_000_000)]


def _three_files(tmp_path: Path, monkeypatch):
    _isolate_ingest_log(monkeypatch)
    folder = tmp_path / "log"
    folder.mkdir()
    for n in ("1787863348274", "1787863348275", "1787863348276"):
        shutil.copy(CAPTURE / _NAME, folder / n)
    events, stats = swim_file_ingest.scan_swim_folder(
        folder, processed_dir=tmp_path / "p", failed_dir=tmp_path / "f", defer_archive=True)
    assert len(events) == 3 and len(stats.pending) == 3
    return folder, events, stats


def test_one_poison_file_is_quarantined_and_the_rest_are_stored(tmp_path, monkeypatch):
    folder, events, stats = _three_files(tmp_path, monkeypatch)
    calls: list[int] = []
    stored: list[int] = []

    def store(batch):
        calls.append(len(batch))
        if len(calls) == 2:                             # the second file is the poisoned one
            raise asyncpg.exceptions.NumericValueOutOfRangeError("integer out of range")
        stored.append(len(batch))

    swim_file_ingest.isolate_poison(events, stats, store, asyncpg.exceptions.NumericValueOutOfRangeError("integer out of range"))

    assert calls == [1, 1, 1] and stored == [1, 1], "each file was tried alone; the second failed"
    assert (tmp_path / "f" / "1787863348275").exists(), "the offender is in failed/"
    assert swim_file_ingest.commit_scan(stats) == 2
    assert sorted(p.name for p in (tmp_path / "p").iterdir()) == ["1787863348274", "1787863348276"]
    assert stats.files_failed == 1 and stats.files_processed == 2
    assert not list((folder / swim_file_ingest.INFLIGHT_DIR).iterdir())


def test_a_database_outage_is_not_poison_and_nothing_is_quarantined(tmp_path, monkeypatch):
    folder, events, stats = _three_files(tmp_path, monkeypatch)

    def store(batch):
        raise ConnectionError("database unavailable")

    with pytest.raises(ConnectionError):
        swim_file_ingest.isolate_poison(events, stats, store, ConnectionError("database unavailable"))

    assert sorted(p.name for p in folder.iterdir() if p.is_file()) == ["1787863348274", "1787863348275", "1787863348276"]
    assert not (tmp_path / "f").exists() or not list((tmp_path / "f").iterdir()), "no good file was judged bad"


def test_an_outage_part_way_through_the_isolation_gives_everything_back(tmp_path, monkeypatch):
    folder, events, stats = _three_files(tmp_path, monkeypatch)
    calls = []

    def store(batch):
        calls.append(len(batch))
        if len(calls) == 2:
            raise ConnectionError("database went away")

    with pytest.raises(ConnectionError):
        swim_file_ingest.isolate_poison(events, stats, store, asyncpg.exceptions.DataError("bad"))
    assert sorted(p.name for p in folder.iterdir() if p.is_file()) == ["1787863348274", "1787863348275", "1787863348276"]


def test_poison_is_told_from_an_outage():
    assert swim_file_ingest.is_poison(asyncpg.exceptions.NumericValueOutOfRangeError("x"))
    assert swim_file_ingest.is_poison(asyncpg.exceptions.CharacterNotInRepertoireError("x"))
    assert swim_file_ingest.is_poison(ValueError("x"))
    assert not swim_file_ingest.is_poison(ConnectionError("x"))
    assert not swim_file_ingest.is_poison(asyncpg.exceptions.PostgresConnectionError("x"))
    assert not swim_file_ingest.is_poison(TimeoutError())


def test_with_no_scan_files_behind_the_events_the_error_is_raised_again(tmp_path):
    with pytest.raises(asyncpg.exceptions.DataError):
        swim_file_ingest.isolate_poison([_event()], swim_file_ingest.IngestStats(), lambda e: None,
                                        asyncpg.exceptions.DataError("x"))


# ── R-06 ───────────────────────────────────────────────────────────────────────

def test_alert_origins_are_found_without_scanning_every_event_for_every_alert():
    """Every event is over the latency threshold, so every event raises an alert: 6,000 alerts over 6,000 events."""
    now = datetime.now(timezone.utc)
    events = [_event(event_id=f"e{i}", service_name="ISMC", latency_ms=90_000.0, event_time_utc=now, received_time_utc=now)
              for i in range(6_000)]
    started = time.perf_counter()
    alerts = check_alerts(events)
    elapsed = time.perf_counter() - started
    assert len(alerts) >= 6_000 and {a.data_origin for a in alerts} == {"live"}
    assert elapsed < 3.0, f"{elapsed:.1f} s: the lookup is quadratic again"


def test_aggregate_alerts_still_read_mixed_and_unknown_origins():
    now = datetime.now(timezone.utc)
    mixed = [_event(event_id=f"m{i}", service_name="SMES", message_type="SURFACE_MOVEMENT", track_id="T1",
                    runway=None, rvr_touchdown=None, surface_event_type="TAXI_OUT",
                    data_origin="live" if i % 2 else "synthetic", event_time_utc=now, received_time_utc=now)
             for i in range(40)]
    spikes = [a for a in check_alerts(mixed) if a.alert_type == "ALERT_SURFACE_EVENT_SPIKE"]
    assert spikes and spikes[0].data_origin == "mixed"
