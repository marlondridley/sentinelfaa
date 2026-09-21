import json
from pathlib import Path

import pytest

from src import storage
from src.swim_file_ingest import scan_swim_folder


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
