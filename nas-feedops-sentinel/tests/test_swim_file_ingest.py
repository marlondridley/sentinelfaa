import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import storage
from src.swim_file_ingest import _normalise_record, scan_swim_folder

_EXPECTED_UTC = datetime(2026, 8, 27, 20, 42, 16, 123000, tzinfo=timezone.utc)


@pytest.mark.parametrize("stamp", [
    "2026-08-27T20:42:16.123Z",
    "2026-08-27T16:42:16.123-04:00",
    "2026-08-27T20:42:16.123",
    datetime(2026, 8, 27, 20, 42, 16, 123000),
])
def test_event_time_is_utc_aware_to_the_microsecond(stamp):
    """Offsets convert to UTC, offset-less values are UTC, and fractional seconds reach latency_ms."""
    event, err = _normalise_record({"airport_code": "KDCA", "tracon": "PCT", "service_name": "ISMC",
                                    "message_type": "m", "event_time_utc": stamp}, "x.json", index=0)
    assert event is not None, err
    assert event.event_time_utc == _EXPECTED_UTC
    assert event.received_time_utc.utcoffset() == timedelta(0)
    assert event.latency_ms == (event.received_time_utc - _EXPECTED_UTC).total_seconds() * 1_000


def test_a_scan_writes_its_ingest_log_in_one_batch(tmp_path: Path, monkeypatch):
    """One database write per scan, not per file: per-file inserts were a quarter of scan time."""
    from src import swim_file_ingest

    real_write = swim_file_ingest._append_log_entries
    batches: list[int] = []
    monkeypatch.setattr(swim_file_ingest, "_append_log_entries",
                        lambda entries: (batches.append(len(entries)), real_write(entries)))
    source_dir = tmp_path / "swim_live"
    source_dir.mkdir()
    for n in range(3):
        (source_dir / f"179000000000{n}.json").write_text(json.dumps({
            "airport_code": "KDCA", "service_name": "TERMINAL_AUTO",
            "message_type": "TAIS_track", "aircraft_id": "AAL123"}))
    (source_dir / "1790000000009.json").write_text("{not json")

    scan_swim_folder(source_dir=source_dir, processed_dir=tmp_path / "processed", failed_dir=tmp_path / "failed")

    assert batches == [4]
    logged = {r["file"]: r["status"] for r in swim_file_ingest.read_ingest_log(10)}
    assert logged == {"1790000000000.json": "processed", "1790000000001.json": "processed",
                      "1790000000002.json": "processed", "1790000000009.json": "failed"}


def test_a_backlog_is_taken_newest_first_by_the_arrival_time_in_the_name(tmp_path: Path):
    """The kit names files by arrival (epoch millis), so no stat per file is needed to order them."""
    source_dir = tmp_path / "swim_live"
    source_dir.mkdir()
    for name in ("1790000000002.json", "requeue_1780000000000.json", "1790000000003.json",
                 "notes.json", "1790000000001.json"):
        (source_dir / name).write_text(json.dumps({
            "airport_code": "KDCA", "service_name": "TERMINAL_AUTO",
            "message_type": "TAIS_track", "aircraft_id": "AAL123"}))

    first, _ = scan_swim_folder(source_dir=source_dir, processed_dir=tmp_path / "processed",
                                failed_dir=tmp_path / "failed", max_files=3)
    rest, _ = scan_swim_folder(source_dir=source_dir, processed_dir=tmp_path / "processed",
                               failed_dir=tmp_path / "failed", max_files=3)

    assert [Path(e.raw_message_path).name for e in first] == [
        "1790000000003.json", "1790000000002.json", "1790000000001.json"]
    assert [Path(e.raw_message_path).name for e in rest] == ["requeue_1780000000000.json", "notes.json"]


def test_latency_is_measured_at_kit_receipt_not_at_scan(tmp_path: Path):
    """Time a file spends waiting for the watcher (a restart, a backlog) is not feed latency."""
    source_dir = tmp_path / "swim_live"
    source_dir.mkdir()
    msg = source_dir / "queued_msg.json"
    event_time = datetime.now(timezone.utc) - timedelta(minutes=10, seconds=2)
    msg.write_text(json.dumps({"airport_code": "KDCA", "service_name": "TERMINAL_AUTO",
                               "message_type": "TAIS_track", "aircraft_id": "AAL123",
                               "event_time_utc": event_time.isoformat()}))
    received = event_time + timedelta(seconds=2)  # the kit got it 2 s after the event...
    os.utime(msg, (received.timestamp(), received.timestamp()))  # ...then it waited 10 minutes

    events, _ = scan_swim_folder(source_dir=source_dir, processed_dir=tmp_path / "processed",
                                 failed_dir=tmp_path / "failed")

    assert len(events) == 1
    assert abs((events[0].received_time_utc - received).total_seconds()) < 0.01
    assert 1_900 < events[0].latency_ms < 2_100


@pytest.fixture(autouse=True)
def isolated_db():
    """
    Reset the database before each test, matching the convention in the
    rest of the suite.

    Without this these tests passed exactly once against a fresh database
    and failed on every run after. scan_swim_folder() skips files already
    recorded in ingest_log, and ingest_log is keyed by FILE NAME, not by
    path -- so the fixed names used here (sample_msg.json, bom_msg.json,
    ...) were treated as already-processed on the second run even though
    each test writes them into its own tmp_path. A test that only passes
    on a virgin database is not evidence of anything.
    """
    storage.override_db_path(":memory:")
    yield
    storage.override_db_path(":memory:")


def test_scan_swim_folder(tmp_path: Path):
    source_dir = tmp_path / "swim_live"
    processed_dir = tmp_path / "processed"
    source_dir.mkdir()

    test_file = source_dir / "sample_msg.json"
    test_file.write_text(
        json.dumps(
            {
                "airport_code": "KDCA",
                "service_name": "TERMINAL_AUTO",
                "message_type": "TAIS_track",
                "aircraft_id": "AAL123",
            }
        )
    )

    events, stats = scan_swim_folder(source_dir=source_dir, processed_dir=processed_dir)

    assert len(events) == 1
    assert stats.files_processed == 1
    assert stats.files_skipped == 0


def test_scan_is_idempotent(tmp_path: Path):
    source_dir = tmp_path / "swim_live"
    processed_dir = tmp_path / "processed"
    source_dir.mkdir()

    test_file = source_dir / "idempotent_msg.json"
    test_file.write_text(
        json.dumps(
            {
                "airport_code": "KIAD",
                "service_name": "TERMINAL_AUTO",
                "message_type": "TAIS_track",
                "aircraft_id": "UAL456",
            }
        )
    )

    # First run: process file
    events1, stats1 = scan_swim_folder(
        source_dir=source_dir, processed_dir=processed_dir
    )
    assert len(events1) == 1
    assert stats1.files_processed == 1

    # Second run: attempt to process same file name in source
    test_file.write_text(json.dumps({"airport_code": "KIAD"}))
    events2, stats2 = scan_swim_folder(
        source_dir=source_dir, processed_dir=processed_dir
    )
    assert len(events2) == 0
    assert stats2.files_skipped == 1


def test_utf8_bom_file(tmp_path: Path):
    source_dir = tmp_path / "swim_live"
    processed_dir = tmp_path / "processed"
    source_dir.mkdir()

    test_file = source_dir / "bom_msg.json"
    content = json.dumps(
        {
            "airport_code": "KBWI",
            "service_name": "TERMINAL_AUTO",
            "message_type": "TAIS_track",
            "aircraft_id": "SWA789",
        }
    )
    # Write with UTF-8 BOM header \ufeff
    test_file.write_bytes("\ufeff".encode("utf-8") + content.encode("utf-8"))

    events, stats = scan_swim_folder(source_dir=source_dir, processed_dir=processed_dir)
    assert len(events) == 1
    assert events[0].airport_code == "KBWI"
    assert stats.files_processed == 1


def test_read_only_mount_resilience(tmp_path: Path, monkeypatch):
    source_dir = tmp_path / "swim_live"
    processed_dir = tmp_path / "processed"
    source_dir.mkdir()

    test_file = source_dir / "readonly_msg.json"
    test_file.write_text(
        json.dumps(
            {
                "airport_code": "KADW",
                "service_name": "TERMINAL_AUTO",
                "message_type": "TAIS_track",
                "aircraft_id": "AF1",
            }
        )
    )

    # Simulate read-only filesystem by raising OSError on shutil.move
    def mock_move(src, dst):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr("shutil.move", mock_move)

    # First run: process file on simulated read-only mount
    events1, stats1 = scan_swim_folder(
        source_dir=source_dir, processed_dir=processed_dir
    )
    assert len(events1) == 1
    assert stats1.files_processed == 1

    # Second run: file remains in source_dir because move was skipped, but should be skipped as already seen
    events2, stats2 = scan_swim_folder(
        source_dir=source_dir, processed_dir=processed_dir
    )
    assert len(events2) == 0
    assert stats2.files_skipped == 1


# ── One bad file must never take the watcher down ────────────────────────────
#
# nas-sentinel crash-looped because the scanner treated the capture folder's
# .gitkeep as a message (the kit writes extensionless files, and a dotfile has
# no extension either), the resulting record had event_time_utc=None, and
# `received - None` raised TypeError straight out of scan_swim_folder().


def _valid_message(source_dir: Path, name: str = "valid_msg.json") -> None:
    (source_dir / name).write_text(json.dumps({
        "airport_code": "KDCA",
        "service_name": "TERMINAL_AUTO",
        "message_type": "TAIS_track",
        "aircraft_id": "AAL123",
    }))


def test_dotfiles_in_the_ingest_folder_are_ignored(tmp_path: Path):
    source_dir, processed_dir, failed_dir = (tmp_path / d for d in ("in", "processed", "failed"))
    source_dir.mkdir()
    (source_dir / ".gitkeep").write_text("")
    _valid_message(source_dir)

    events, stats = scan_swim_folder(
        source_dir=source_dir, processed_dir=processed_dir, failed_dir=failed_dir
    )

    assert stats.files_detected == 1
    assert len(events) == 1
    assert (source_dir / ".gitkeep").exists(), "placeholder must be left where it is"


def test_record_with_null_event_time_does_not_crash_the_scan(tmp_path: Path):
    source_dir, processed_dir, failed_dir = (tmp_path / d for d in ("in", "processed", "failed"))
    source_dir.mkdir()
    (source_dir / "null_time.json").write_text(json.dumps({
        "airport_code": "KDCA",
        "service_name": "TERMINAL_AUTO",
        "message_type": "TAIS_track",
        "aircraft_id": "AAL123",
        "event_time_utc": None,
    }))
    _valid_message(source_dir)

    events, stats = scan_swim_folder(
        source_dir=source_dir, processed_dir=processed_dir, failed_dir=failed_dir
    )

    assert stats.files_processed + stats.files_failed == 2
    assert any(e.aircraft_id == "AAL123" for e in events)


def test_unexpected_error_in_one_file_fails_that_file_not_the_scan(tmp_path: Path, monkeypatch):
    import src.swim_file_ingest as ingest

    source_dir, processed_dir, failed_dir = (tmp_path / d for d in ("in", "processed", "failed"))
    source_dir.mkdir()
    _valid_message(source_dir, "a_poison.json")
    _valid_message(source_dir, "b_good.json")

    real = ingest._normalise_record

    def poisoned(raw, source_file, **kwargs):
        if source_file.endswith("a_poison.json"):
            raise TypeError("boom")
        return real(raw, source_file, **kwargs)

    monkeypatch.setattr(ingest, "_normalise_record", poisoned)

    events, stats = scan_swim_folder(
        source_dir=source_dir, processed_dir=processed_dir, failed_dir=failed_dir
    )

    assert stats.files_failed == 1
    assert stats.files_processed == 1
    assert len(events) == 1
    assert (failed_dir / "a_poison.json").exists()
