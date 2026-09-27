"""
The Kit folder is a queue with a janitor that deletes by age, ingested or not. How near the oldest waiting
message is to that age has to be visible before it is crossed, and what the janitor did drop has to be counted.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from src import swim_file_ingest as ingest
from src.maintenance import purge_old_kit_files
from src.metrics import SWIM_FILES_DROPPED_UNPROCESSED, SWIM_FOLDER_BACKLOG, SWIM_FOLDER_OLDEST_WAITING_SECONDS
from src.swim_file_ingest import FolderSource


@pytest.fixture(autouse=True)
def nothing_ingested_yet(monkeypatch):
    monkeypatch.setattr(ingest, "_get_already_processed", lambda names: set())


def _record(folder: Path, seq: int, age_s: float) -> Path:
    path = folder / f"swim_ab12_{seq:06d}_{int((time.time() - age_s) * 1000)}.swimmsg"
    path.write_text("{}", encoding="utf-8")
    return path


def _poll(folder: Path, cap: int = 500):
    source = FolderSource(folder, folder / "processed", folder / "failed")
    batch = source.poll(cap)
    return batch


def test_the_backlog_and_its_oldest_message_are_measured_on_every_poll(tmp_path):
    for seq, age in enumerate((200, 30, 1), start=1):
        _record(tmp_path, seq, age)

    _poll(tmp_path)

    assert SWIM_FOLDER_BACKLOG._value.get() == 3
    assert SWIM_FOLDER_OLDEST_WAITING_SECONDS._value.get() == pytest.approx(200, abs=5)


def test_the_gauge_counts_what_is_waiting_not_what_this_scan_takes(tmp_path):
    for seq in range(1, 6):
        _record(tmp_path, seq, seq)

    _poll(tmp_path, cap=2)

    assert SWIM_FOLDER_BACKLOG._value.get() == 5, "a capped scan must not hide the rest of the backlog"


def test_an_ingested_message_no_longer_counts_as_waiting(tmp_path, monkeypatch):
    done = _record(tmp_path, 1, 500)
    _record(tmp_path, 2, 10)
    monkeypatch.setattr(ingest, "_get_already_processed", lambda names: {done.name})

    _poll(tmp_path)

    assert SWIM_FOLDER_BACKLOG._value.get() == 1
    assert SWIM_FOLDER_OLDEST_WAITING_SECONDS._value.get() == pytest.approx(10, abs=5)


def test_an_empty_folder_reads_zero_not_the_last_value(tmp_path):
    _record(tmp_path, 1, 100)
    _poll(tmp_path)
    for f in tmp_path.glob("*.swimmsg"):
        f.unlink()

    _poll(tmp_path)

    assert SWIM_FOLDER_BACKLOG._value.get() == 0 and SWIM_FOLDER_OLDEST_WAITING_SECONDS._value.get() == 0


def test_a_file_named_without_an_arrival_time_is_counted_but_cannot_age(tmp_path):
    (tmp_path / "message_without_a_timestamp.json").write_text("{}", encoding="utf-8")

    _poll(tmp_path)

    assert SWIM_FOLDER_BACKLOG._value.get() == 1 and SWIM_FOLDER_OLDEST_WAITING_SECONDS._value.get() == 0


def _age(path: Path, minutes: float) -> None:
    then = time.time() - minutes * 60
    os.utime(path, (then, then))


def test_the_janitor_counts_the_messages_it_drops_and_only_those(tmp_path):
    old_record, fresh = _record(tmp_path, 1, 0), _record(tmp_path, 2, 0)
    old_kit_file = tmp_path / "1787863346003"
    old_kit_file.write_text("x", encoding="utf-8")
    kit_log = tmp_path / "messages.log"
    kit_log.write_text("x", encoding="utf-8")
    for f in (old_record, old_kit_file, kit_log):
        _age(f, 10)
    before = SWIM_FILES_DROPPED_UNPROCESSED._value.get()

    removed, remaining = purge_old_kit_files(tmp_path, max_age_minutes=5)

    assert (removed, remaining) == (3, 1) and fresh.exists()
    assert SWIM_FILES_DROPPED_UNPROCESSED._value.get() - before == 2, "the kit's own log is not a dropped message"


def test_a_janitor_pass_that_drops_nothing_counts_nothing(tmp_path):
    _record(tmp_path, 1, 0)
    before = SWIM_FILES_DROPPED_UNPROCESSED._value.get()

    assert purge_old_kit_files(tmp_path, max_age_minutes=5) == (0, 1)
    assert SWIM_FILES_DROPPED_UNPROCESSED._value.get() == before
