"""
Ingest reads through a small source interface (src/raw_message.py: poll / commit /
quarantine), and the folder of files the Java edge writes is one implementation of it.

These tests are the Kafka-swap contract: a source that is not a folder at all drives
the same pipeline, and a FolderSource over the Java writer's own records (the fixtures
in tests/fixtures/swim_raw/) yields the same events as the stock kit's captured files.
No database: the ingest log is stubbed, as in test_swim_kit_ingest.py.
"""
from __future__ import annotations

import dataclasses
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import swim_file_ingest as ingest
from src.raw_message import (
    RawMessage,
    deliveries,
    envelope_json,
    message_from_envelope,
    message_from_kit_text,
)
from src.swim_file_ingest import (
    FolderSource,
    MessageDelivery,
    _parse_file,
    commit_scan,
    ingest_batch,
    message_files,
    records_from_raw,
    scan_swim_folder,
)

TESTS = Path(__file__).parent
CAPTURE = TESTS / "fixtures" / "swim_kit" / "capture_2026-08-27"
RECORDS = TESTS / "fixtures" / "swim_raw" / "capture_2026-08-27"
CAPTURES = sorted(p for p in CAPTURE.iterdir() if p.is_file())
RECORD_FILES = sorted(RECORDS.glob("*.swimmsg"))
RECEIVED = datetime(2026, 8, 27, 20, 42, 25, tzinfo=timezone.utc)
TDLS = CAPTURE / "1787863348274"        # a departure clearance: one KBWI event


@pytest.fixture(autouse=True)
def log(monkeypatch):
    """Nothing seen before, and the ingest log's entries captured instead of written."""
    entries: list[dict] = []
    monkeypatch.setattr(ingest, "_get_already_processed", lambda names: set())
    monkeypatch.setattr(ingest, "_append_log_entries", entries.extend)
    return entries


def tdls(**headers) -> RawMessage:
    message = message_from_kit_text(TDLS.read_text(encoding="utf-8-sig"), "tdls-1", RECEIVED)
    return dataclasses.replace(message, headers={**message.headers, **headers})


class FakeSource:
    """Not a folder: what a broker-backed source (Kafka, say) would be to the pipeline."""

    def __init__(self, messages: list[RawMessage]):
        self.messages = messages
        self.polled_with: list[int] = []
        self.committed: list[str] = []
        self.quarantined: list[tuple[str, str]] = []

    def poll(self, max_messages):
        self.polled_with.append(max_messages)
        return deliveries([MessageDelivery(m) for m in self.messages[:max_messages]])

    def commit(self, delivery):
        self.committed.append(delivery.name)

    def quarantine(self, delivery, reason):
        self.quarantined.append((delivery.name, reason))


# ── a source that is not a folder ─────────────────────────────────────────────

def test_a_source_that_is_not_a_folder_drives_the_same_pipeline(log):
    source = FakeSource([tdls()])

    events, stats = ingest_batch(source)

    assert [e.service_name for e in events] == ["TOWER_DEPARTURE"]
    assert events[0].airport_code == "KBWI"
    assert events[0].received_time_utc == RECEIVED         # the source's receipt time, not the scan's
    assert source.committed == ["tdls-1"] and source.quarantined == []
    assert (stats.files_detected, stats.files_processed, stats.files_failed) == (1, 1, 0)
    assert [(e["file"], e["status"], e["event_count"]) for e in log] == [("tdls-1", "processed", 1)]


def test_the_cap_is_the_sources_to_apply():
    source = FakeSource([tdls()])
    ingest_batch(source, max_messages=7)
    assert source.polled_with == [7]


def test_a_deferred_commit_waits_until_the_events_are_stored(log):
    source = FakeSource([tdls()])

    events, stats = ingest_batch(source, defer_commit=True)

    assert events and source.committed == [] and log == []          # nothing acknowledged yet
    assert commit_scan(stats) == 1
    assert source.committed == ["tdls-1"]
    assert [e["status"] for e in log] == ["processed"]
    assert commit_scan(stats) == 0                                    # committing twice is a no-op


def test_a_message_that_can_never_succeed_is_quarantined_with_the_reason(log):
    bad = RawMessage("XYZ:1:1", {"mex": "XYZ", "msgType": "ZZ"}, '{"unknownRoot": {"a": 1}}', RECEIVED, "bad-1")
    source = FakeSource([bad, tdls()])

    events, stats = ingest_batch(source)

    assert [name for name, _ in source.quarantined] == ["bad-1"]
    assert "Unrecognised STDDS payload shape" in source.quarantined[0][1]
    assert source.committed == ["tdls-1"] and len(events) == 1          # one bad message never stops the rest
    assert (stats.files_processed, stats.files_failed) == (1, 1)
    assert [e["status"] for e in log] == ["failed", "processed"]


def test_a_payload_the_edge_could_not_convert_is_quarantined_not_guessed_at():
    unconverted = dataclasses.replace(
        tdls(), body="<not-json/>", convert_error="payload converted to an empty JSON object", source_name="raw-1")
    source = FakeSource([unconverted])

    events, _ = ingest_batch(source)

    assert events == []
    assert source.quarantined and "could not be converted to JSON" in source.quarantined[0][1]


def test_a_withheld_message_is_committed_so_it_is_never_delivered_again(log):
    source = FakeSource([dataclasses.replace(tdls(blockAtFAA="true"), source_name="held-1")])

    events, stats = ingest_batch(source)

    assert events == [] and source.quarantined == []
    assert source.committed == ["held-1"]
    assert stats.files_withheld == 1
    assert [(e["status"], e["event_count"]) for e in log] == [("withheld", 0)]


def test_a_redelivered_message_is_the_same_events():
    first = ingest_batch(FakeSource([tdls()]))[0]
    again = ingest_batch(FakeSource([dataclasses.replace(tdls(), received_at=RECEIVED.replace(minute=59))]))[0]

    assert [e.event_id for e in first] == [e.event_id for e in again]
    assert first[0].received_time_utc != again[0].received_time_utc


def test_a_requested_shutdown_leaves_the_rest_undelivered():
    source = FakeSource([tdls(), tdls()])
    ingest._shutdown_requested.set()
    try:
        events, stats = ingest_batch(source)
    finally:
        ingest._shutdown_requested.clear()
    assert events == [] and source.committed == [] and stats.files_processed == 0


# ── the folder is one implementation of the interface ─────────────────────────

def _scan_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    src, processed, failed = tmp_path / "in", tmp_path / "processed", tmp_path / "failed"
    src.mkdir()
    return src, processed, failed


def test_the_java_writers_records_and_the_stock_kit_files_parse_identically():
    for record, capture in zip(RECORD_FILES, CAPTURES):
        from_record, record_error = _parse_file(record)
        from_capture, capture_error = _parse_file(capture)
        assert record_error is None and capture_error is None, record.name
        assert from_record == from_capture, record.name


def test_a_folder_of_java_records_ingests_ten_for_ten(tmp_path, log):
    src, processed, failed = _scan_dirs(tmp_path)
    for record in RECORD_FILES:
        shutil.copy(record, src / record.name)

    events, stats = scan_swim_folder(src, processed_dir=processed, failed_dir=failed)

    assert stats.files_failed == 0 and list(failed.iterdir()) == []
    assert len(list(processed.iterdir())) == 10 and stats.files_processed == 10
    assert [p for p in src.iterdir() if p.is_file()] == []
    assert events and all(e.raw_message_path.endswith(".swimmsg") for e in events)
    assert sorted(e["file"] for e in log) == sorted(p.name for p in RECORD_FILES)


def test_records_and_stock_files_of_the_same_messages_are_the_same_events(tmp_path):
    def scan(files: list[Path], name: str):
        base = tmp_path / name
        base.mkdir()
        src, processed, failed = _scan_dirs(base)
        for f in files:
            shutil.copy(f, src / f.name)
        return scan_swim_folder(src, processed_dir=processed, failed_dir=failed)[0]

    from_records = scan(RECORD_FILES, "records")
    from_captures = scan(CAPTURES, "captures")

    def shape(events):
        return sorted((e.event_id, e.service_name, e.airport_code, e.aircraft_id, str(e.event_time_utc)) for e in events)

    assert shape(from_records) == shape(from_captures) and from_records


def test_a_records_receipt_time_is_the_edges_not_the_files(tmp_path):
    src, processed, failed = _scan_dirs(tmp_path)
    record = RECORD_FILES[1]
    shutil.copy(record, src / record.name)
    stated = message_from_envelope(record.read_text(encoding="utf-8")).received_at

    events, _ = scan_swim_folder(src, processed_dir=processed, failed_dir=failed)

    assert events and {e.received_time_utc for e in events} == {stated}


def test_a_record_the_edge_could_not_convert_goes_to_failed_with_its_reason(tmp_path):
    src, processed, failed = _scan_dirs(tmp_path)
    bad = RawMessage("sha256:ab", {"mex": "TDES"}, "not xml", RECEIVED, convert_error="payload converted to an empty JSON object")
    (src / "swim_dead_000001_1790000000001.swimmsg").write_text(envelope_json(bad), encoding="utf-8")

    events, stats = scan_swim_folder(src, processed_dir=processed, failed_dir=failed)

    assert events == [] and stats.files_failed == 1
    assert [p.name for p in failed.iterdir()] == ["swim_dead_000001_1790000000001.swimmsg"]


@pytest.mark.parametrize("text", ['{"format": "swim-raw/9"}', "not json at all", ""])
def test_a_record_that_cannot_be_read_goes_to_failed(tmp_path, text):
    src, processed, failed = _scan_dirs(tmp_path)
    (src / "swim_x_000001_1790000000001.swimmsg").write_text(text, encoding="utf-8")

    events, stats = scan_swim_folder(src, processed_dir=processed, failed_dir=failed)

    assert events == [] and stats.files_failed == 1 and len(list(failed.iterdir())) == 1


def test_a_record_still_being_written_is_not_a_message(tmp_path):
    src = tmp_path
    (src / ".swim_a_000001_1790000000001.swimmsg.tmp").write_text("{", encoding="utf-8")   # the writer's temp file
    (src / ".hidden.swimmsg").write_text("{}", encoding="utf-8")
    (src / "swim_a_000002_1790000000002.swimmsg").write_text("{}", encoding="utf-8")

    assert [p.name for p in message_files(src)] == ["swim_a_000002_1790000000002.swimmsg"]


def test_the_newest_arrivals_are_taken_first_across_writer_runs(tmp_path):
    src, processed, failed = _scan_dirs(tmp_path)
    for name, millis in (("aaaa", 1790000000001), ("zzzz", 1790000000002), ("mmmm", 1790000000003)):
        shutil.copy(RECORD_FILES[1], src / f"swim_{name}_000001_{millis}.swimmsg")

    _, stats = scan_swim_folder(src, max_files=2, processed_dir=processed, failed_dir=failed)

    assert sorted(p.name for p in processed.iterdir()) == [
        "swim_mmmm_000001_1790000000003.swimmsg", "swim_zzzz_000001_1790000000002.swimmsg"]
    assert (stats.files_detected, stats.files_processed, stats.files_skipped) == (3, 2, 1)


def test_a_deferred_folder_commit_archives_only_after_commit_scan(tmp_path, log):
    src, processed, failed = _scan_dirs(tmp_path)
    shutil.copy(RECORD_FILES[1], src / RECORD_FILES[1].name)

    _, stats = scan_swim_folder(src, processed_dir=processed, failed_dir=failed, defer_archive=True)

    assert not processed.exists() or list(processed.iterdir()) == []
    assert (src / ".inflight" / RECORD_FILES[1].name).exists() and log == []
    assert commit_scan(stats) == 1
    assert [p.name for p in processed.iterdir()] == [RECORD_FILES[1].name] and not (src / ".inflight" / RECORD_FILES[1].name).exists()


def test_a_file_another_scanner_claimed_is_skipped_not_failed(tmp_path, monkeypatch):
    src, processed, failed = _scan_dirs(tmp_path)
    for record in RECORD_FILES[:2]:
        shutil.copy(record, src / record.name)
    real_claim = ingest._claim
    monkeypatch.setattr(ingest, "_claim", lambda path: None if path.name == RECORD_FILES[0].name else real_claim(path))

    _, stats = scan_swim_folder(src, processed_dir=processed, failed_dir=failed)

    assert (stats.files_processed, stats.files_failed, stats.files_skipped) == (1, 0, 1)
    assert (src / RECORD_FILES[0].name).exists()                    # untouched, for whoever claimed it


def test_folder_source_speaks_the_whole_interface(tmp_path):
    src, processed, failed = _scan_dirs(tmp_path)
    shutil.copy(RECORD_FILES[1], src / RECORD_FILES[1].name)
    source = FolderSource(src, processed, failed)
    processed.mkdir()
    failed.mkdir()

    batch = source.poll(10)
    (delivery,) = list(batch.deliveries)
    assert (batch.planned, batch.detected) == (1, 1) and delivery.name == RECORD_FILES[1].name
    records, error = delivery.records()
    assert records and error is None
    source.commit(delivery)
    assert [p.name for p in processed.iterdir()] == [RECORD_FILES[1].name]


def test_records_from_raw_is_what_every_source_shares():
    for capture in CAPTURES:
        message = message_from_kit_text(capture.read_text(encoding="utf-8-sig"), capture.name, RECEIVED)
        assert records_from_raw(message) == _parse_file(capture)[0]
