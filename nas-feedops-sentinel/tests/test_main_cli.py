"""
`python -m src.main --mode replay` (one scan, no --watch).

scan_swim_folder() returns (events, stats). The replay branch once bound the
whole tuple to `events`: a 2-tuple is never falsy, so the empty-folder
fallback to replay_from_directory() could not run, and the pipeline was
handed the tuple instead of a list of events. These tests drive main()
itself, with the scanner and the pipeline replaced, so they need no files
and no database.
"""
from __future__ import annotations

import sys

import pytest

import src.main as main
from src.demo_scenario import generate_cascade_failure_scenario
from src.swim_file_ingest import IngestStats


@pytest.fixture
def run_replay(monkeypatch, tmp_path):
    """Run main() in replay mode; return the (events, label) pairs it sent to the pipeline."""
    for side_effect in ("configure_logging", "_install_shutdown_handler", "start_metrics_server"):
        monkeypatch.setattr(main, side_effect, lambda *a, **k: None)
    sent: list[tuple[object, str]] = []
    monkeypatch.setattr(main, "_run_pipeline", lambda events, label="": sent.append((events, label)))
    monkeypatch.setattr(sys, "argv", ["src.main", "--mode", "replay", "--path", str(tmp_path)])

    def _run():
        main.main()
        return sent

    return _run


def test_replay_hands_the_pipeline_the_scanned_events_not_the_tuple(monkeypatch, run_replay):
    events = generate_cascade_failure_scenario()
    monkeypatch.setattr(main, "scan_swim_folder", lambda path, **kw: (events, IngestStats()))
    fallback_called = []
    monkeypatch.setattr(main, "replay_from_directory", lambda path: fallback_called.append(path) or ([], []))

    sent = run_replay()

    assert sent == [(events, "REPLAY")]
    assert isinstance(sent[0][0], list)
    assert not fallback_called


def test_replay_falls_back_to_directory_replay_when_the_scan_finds_nothing(monkeypatch, run_replay):
    events = generate_cascade_failure_scenario()
    monkeypatch.setattr(main, "scan_swim_folder", lambda path, **kw: ([], IngestStats()))
    monkeypatch.setattr(main, "replay_from_directory", lambda path: (events, []))

    assert run_replay() == [(events, "REPLAY")]


def test_replay_with_nothing_anywhere_does_not_run_the_pipeline(monkeypatch, run_replay):
    monkeypatch.setattr(main, "scan_swim_folder", lambda path, **kw: ([], IngestStats()))
    monkeypatch.setattr(main, "replay_from_directory", lambda path: ([], []))

    assert run_replay() == []
