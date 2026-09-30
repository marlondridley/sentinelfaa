"""
Retention: what the feed leaves behind ages out.

The Kit writes ~20 files a second and nothing removed the archive or the events, so a long run filled the
disk and the Kit's writer halted the feed. These tests hold the rule for each of the three places (archived
files, rejected files, live events) and the properties that make it safe to call from every scan: rate
limited, bounded in time, and never raising.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import retention, storage
from src.config import settings
from src.models import AlertEvent, NASEvent


def _aged(directory: Path, name: str, hours_old: float) -> Path:
    path = directory / name
    path.write_text("x", encoding="utf-8")
    when = time.time() - hours_old * 3600
    os.utime(path, (when, when))
    return path


@pytest.fixture(autouse=True)
def _fresh_throttle(monkeypatch):
    """Each test starts as if no pass had ever run, with the defaults in force."""
    monkeypatch.setattr(retention, "_last_run", None)
    monkeypatch.setattr(settings, "PROCESSED_RETENTION_HOURS", 6)
    monkeypatch.setattr(settings, "FAILED_RETENTION_DAYS", 7)
    monkeypatch.setattr(settings, "LIVE_EVENT_RETENTION_DAYS", 0)      # files only, unless a test turns events on
    monkeypatch.setattr(settings, "RETENTION_INTERVAL_MINUTES", 5)
    monkeypatch.setattr(settings, "RETENTION_BUDGET_SECONDS", 20.0)


# ── Files ─────────────────────────────────────────────────────────────────────

def test_only_files_older_than_the_limit_are_removed(tmp_path: Path):
    old, fresh = _aged(tmp_path, "old", 10), _aged(tmp_path, "fresh", 1)

    removed = retention.prune_directory(tmp_path, timedelta(hours=6), deadline=time.monotonic() + 30)

    assert removed == 1
    assert not old.exists() and fresh.exists()


def test_dotfiles_and_subdirectories_are_left_alone(tmp_path: Path):
    """.gitkeep and the ingest folder's own bookkeeping are not archive."""
    keep = _aged(tmp_path, ".gitkeep", 100)
    (tmp_path / ".inflight").mkdir()
    inside = _aged(tmp_path / ".inflight", "claimed", 100)
    sub = tmp_path / "sub"
    sub.mkdir()
    nested = _aged(sub, "nested", 100)

    assert retention.prune_directory(tmp_path, timedelta(hours=6), deadline=time.monotonic() + 30) == 0
    assert keep.exists() and inside.exists() and nested.exists()


def test_a_missing_folder_is_nothing_to_do(tmp_path: Path):
    assert retention.prune_directory(tmp_path / "absent", timedelta(hours=1), deadline=time.monotonic() + 30) == 0


def test_a_pass_stops_at_its_deadline_and_leaves_the_rest_for_next_time(tmp_path: Path):
    for i in range(5):
        _aged(tmp_path, f"m{i}", 10)

    assert retention.prune_directory(tmp_path, timedelta(hours=6), deadline=time.monotonic() - 1) == 0
    assert len(list(tmp_path.iterdir())) == 5


def test_a_pass_removes_archived_and_rejected_files_by_their_own_limits(tmp_path: Path):
    processed, failed = tmp_path / "processed", tmp_path / "failed"
    processed.mkdir(), failed.mkdir()
    old_processed, new_processed = _aged(processed, "a", 7), _aged(processed, "b", 5)
    # 7 h is far inside the 7-day limit for rejected files, 8 days is not
    recent_failed, old_failed = _aged(failed, "c", 7), _aged(failed, "d", 8 * 24)

    result = retention.run_due(processed, failed)

    assert result == {"processed_files": 1, "failed_files": 1, "live_events": 0}
    assert not old_processed.exists() and new_processed.exists()
    assert recent_failed.exists() and not old_failed.exists()


def test_a_limit_of_zero_turns_that_rule_off(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "PROCESSED_RETENTION_HOURS", 0)
    processed, failed = tmp_path / "p", tmp_path / "f"
    processed.mkdir(), failed.mkdir()
    old = _aged(processed, "a", 1000)

    assert retention.run_due(processed, failed)["processed_files"] == 0
    assert old.exists()


def test_passes_are_rate_limited_and_a_caller_can_force_one(tmp_path: Path):
    processed, failed = tmp_path / "p", tmp_path / "f"
    processed.mkdir(), failed.mkdir()

    assert retention.run_due(processed, failed) is not None
    assert retention.run_due(processed, failed) is None, "a scan that arrives inside the interval does nothing"
    _aged(processed, "late", 10)
    assert retention.run_due(processed, failed, force=True)["processed_files"] == 1


def test_a_pass_that_is_already_running_is_not_started_twice(tmp_path: Path):
    assert retention._lock.acquire(blocking=False)
    try:
        assert retention.run_due(tmp_path, tmp_path, force=True) is None
    finally:
        retention._lock.release()


def test_a_failing_event_prune_never_raises_and_does_not_stop_the_file_prune(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "LIVE_EVENT_RETENTION_DAYS", 7)
    def db_down(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(retention, "prune_live_events", db_down)
    processed, failed = tmp_path / "p", tmp_path / "f"
    processed.mkdir(), failed.mkdir()
    old = _aged(processed, "a", 10)

    result = retention.run_due(processed, failed)

    assert result["processed_files"] == 1 and not old.exists()


def test_what_a_pass_removed_is_counted(tmp_path: Path):
    from prometheus_client import REGISTRY

    processed, failed = tmp_path / "p", tmp_path / "f"
    processed.mkdir(), failed.mkdir()
    before = REGISTRY.get_sample_value("nas_retention_pruned_total", {"kind": "processed_files"}) or 0.0
    _aged(processed, "a", 10), _aged(processed, "b", 10)

    retention.run_due(processed, failed)

    assert REGISTRY.get_sample_value("nas_retention_pruned_total", {"kind": "processed_files"}) == before + 2


# ── Events ────────────────────────────────────────────────────────────────────

@pytest.fixture
def db():
    storage.override_db_path(":memory:")
    yield storage
    storage.override_db_path(":memory:")


def _event(origin: str, days_old: float, *, airport: str = "KDCA") -> NASEvent:
    when = datetime.now(timezone.utc) - timedelta(days=days_old)
    return NASEvent(airport_code=airport, tracon="PCT", service_name="ISMC", message_type="STATUS",
                    event_time_utc=when, received_time_utc=when, latency_ms=10.0, feed_status="operational",
                    validation_status="PASSED", data_origin=origin)


async def _ids(db) -> set[str]:
    pool = await db.get_db_pool()
    return {r["event_id"] for r in await pool.fetch("SELECT event_id FROM nas_events")}


async def test_old_live_events_age_out_and_recent_ones_stay(db):
    old, recent = _event("live", 10), _event("live", 1)
    await db.save_events([old, recent])

    removed = await db.prune_live_events(datetime.now(timezone.utc) - timedelta(days=7), 1000)

    assert removed == 1
    assert await _ids(db) == {recent.event_id}


async def test_replay_api_and_synthetic_events_are_never_aged_out(db):
    """An operator put them there; the age of a replayed capture says nothing about whether it is wanted."""
    kept = [_event(origin, 400) for origin in ("replay", "api", "synthetic", "unknown")]
    await db.save_events(kept)

    assert await db.prune_live_events(datetime.now(timezone.utc) - timedelta(days=7), 1000) == 0
    assert await _ids(db) == {e.event_id for e in kept}


async def test_an_event_that_an_alert_points_at_is_evidence_and_stays(db):
    evidence, plain = _event("live", 30), _event("live", 30)
    await db.save_events([evidence, plain])
    await db.save_alerts([AlertEvent(alert_type="ALERT_LOW_RVR", severity="CRITICAL", airport_code="KDCA",
                                     service_name="APDS_RVR", description="Low RVR 200 ft", event_id=evidence.event_id,
                                     data_origin="live")])

    await db.prune_live_events(datetime.now(timezone.utc) - timedelta(days=7), 1000)

    assert await _ids(db) == {evidence.event_id}
    pool = await db.get_db_pool()
    assert await pool.fetchval("SELECT event_id FROM alert_events") == evidence.event_id, "the link was not nulled"


async def test_an_event_that_a_staged_review_points_at_stays(db):
    evidence, plain = _event("live", 30), _event("live", 30)
    await db.save_events([evidence, plain])
    pool = await db.get_db_pool()
    await pool.execute(
        "INSERT INTO staged_actions (action_id, event_id, status, created_at) VALUES ('a1', $1, 'PENDING', now())",
        evidence.event_id)

    await db.prune_live_events(datetime.now(timezone.utc) - timedelta(days=7), 1000)

    assert await _ids(db) == {evidence.event_id}


async def test_one_call_deletes_at_most_the_batch_size(db):
    await db.save_events([_event("live", 30) for _ in range(5)])
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    assert await db.prune_live_events(cutoff, 2) == 2
    assert await db.prune_live_events(cutoff, 2) == 2
    assert await db.prune_live_events(cutoff, 2) == 1
    assert await _ids(db) == set()


async def test_a_pass_works_through_a_backlog_of_events(db, monkeypatch, tmp_path: Path):
    """run_due drives the batches from a scan's thread, through the async bridge."""
    import asyncio

    monkeypatch.setattr(settings, "LIVE_EVENT_RETENTION_DAYS", 7)
    monkeypatch.setattr(retention, "_EVENT_BATCH", 2)
    await db.save_events([_event("live", 30) for _ in range(5)] + [_event("live", 1)])
    processed, failed = tmp_path / "p", tmp_path / "f"

    result = await asyncio.to_thread(retention.run_due, processed, failed)

    assert result["live_events"] == 5
    assert len(await _ids(db)) == 1
