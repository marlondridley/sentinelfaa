"""
`python -m src.main` beyond the single replay scan (tests/test_main_cli.py): the watch mode, the eval mode, the
quarantine on a pipeline failure, the reconciler thread and the shutdown handler. The scanner, the pipeline
and the watcher are replaced, so no files and no database are needed.
"""
from __future__ import annotations

import signal
import sys

import pytest

import src.main as main
from src.swim_file_ingest import IngestStats


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """Run main() with the given arguments; the process-level set-up is replaced."""
    for name in ("configure_logging", "_install_shutdown_handler", "start_metrics_server"):
        monkeypatch.setattr(main, name, lambda *a, **k: None)

    def _run(*args):
        monkeypatch.setattr(sys, "argv", ["src.main", *args])
        main.main()

    return _run


# ── Replay: a failing pipeline is quarantined, parse errors are reported ─────────────────────────────

def test_a_pipeline_failure_in_replay_is_handed_to_the_quarantine(monkeypatch, cli, tmp_path):
    stats = IngestStats()
    isolated: list = []
    committed: list = []

    def _fails(events, label=""):
        raise RuntimeError("poison")

    monkeypatch.setattr(main, "scan_swim_folder", lambda path, **kw: (["e1"], stats))
    monkeypatch.setattr(main, "_run_pipeline", _fails)
    monkeypatch.setattr(main, "isolate_poison", lambda events, s, store, exc: isolated.append((events, s, type(exc))))
    monkeypatch.setattr(main, "commit_scan", committed.append)

    cli("--mode", "replay", "--path", str(tmp_path))

    assert isolated == [(["e1"], stats, RuntimeError)]
    assert committed == [stats], "the scan is still finished: the quarantine decided what happened to each file"


def test_the_quarantines_store_callback_runs_the_pipeline_for_one_file(monkeypatch, cli, tmp_path):
    sent: list = []

    def _run(events, label=""):
        sent.append((events, label))
        if len(sent) == 1:
            raise RuntimeError("poison")

    monkeypatch.setattr(main, "scan_swim_folder", lambda path, **kw: (["e1", "e2"], IngestStats()))
    monkeypatch.setattr(main, "_run_pipeline", _run)
    monkeypatch.setattr(main, "commit_scan", lambda s: None)
    monkeypatch.setattr(main, "isolate_poison", lambda events, s, store, exc: store(["e1"]))

    cli("--mode", "replay", "--path", str(tmp_path))

    assert sent == [(["e1", "e2"], "REPLAY"), (["e1"], "REPLAY")]


def test_parse_errors_in_the_directory_fallback_are_reported_but_the_events_still_run(monkeypatch, cli, tmp_path):
    sent: list = []
    monkeypatch.setattr(main, "scan_swim_folder", lambda path, **kw: ([], IngestStats()))
    monkeypatch.setattr(main, "replay_from_directory", lambda path: (["e1"], ["bad file"]))
    monkeypatch.setattr(main, "_run_pipeline", lambda events, label="": sent.append(events))
    monkeypatch.setattr(main, "commit_scan", lambda s: None)

    cli("--mode", "replay", "--path", str(tmp_path))

    assert sent == [["e1"]]


def test_replay_without_a_path_scans_the_configured_folder(monkeypatch, cli):
    seen: list = []
    monkeypatch.setattr(main, "scan_swim_folder", lambda path, **kw: seen.append(path) or ([], IngestStats()))
    monkeypatch.setattr(main, "replay_from_directory", lambda path: ([], []))
    monkeypatch.setattr(main, "commit_scan", lambda s: None)

    cli("--mode", "replay")

    assert len(seen) == 1 and seen[0] is not None


# ── Watch mode ───────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def watching(monkeypatch, cli, tmp_path):
    """Run main() in --watch mode; return what the watcher was given."""
    given: dict = {}
    monkeypatch.setattr(main, "_start_reconciler", lambda: given.setdefault("reconciler", True))
    monkeypatch.setattr(main, "watch_swim_folder", lambda path, reconcile_interval, on_events: given.update(
        path=path, interval=reconcile_interval, on_events=on_events))
    cli("--mode", "replay", "--watch", "--path", str(tmp_path), "--reconcile-interval", "42")
    return given


def test_watch_mode_starts_the_reconciler_and_the_watcher(watching, tmp_path):
    assert watching["reconciler"] is True
    assert (str(watching["path"]), watching["interval"]) == (str(tmp_path), 42.0)


def test_watched_events_go_to_the_pipeline(monkeypatch, watching):
    sent: list = []
    monkeypatch.setattr(main, "_run_pipeline", lambda events, label="": sent.append((events, label)))

    watching["on_events"](["e1"], IngestStats())

    assert sent == [(["e1"], "REPLAY")]


def test_watched_events_that_fail_are_quarantined_not_dropped(monkeypatch, watching):
    stats = IngestStats()
    isolated: list = []

    def _fails(events, label=""):
        raise RuntimeError("poison")

    monkeypatch.setattr(main, "_run_pipeline", _fails)
    monkeypatch.setattr(main, "isolate_poison", lambda events, s, store, exc: isolated.append((events, s)))

    watching["on_events"](["e1"], stats)

    assert isolated == [(["e1"], stats)]


def test_watch_mode_without_a_path_watches_the_configured_folder(monkeypatch, cli):
    given: dict = {}
    monkeypatch.setattr(main, "_start_reconciler", lambda: None)
    monkeypatch.setattr(main, "watch_swim_folder", lambda path, reconcile_interval, on_events: given.update(path=path))

    cli("--mode", "replay", "--watch")

    assert given["path"] is not None


# ── Eval mode ────────────────────────────────────────────────────────────────────────────────────────

def test_eval_mode_runs_the_evaluator_and_prints_its_report(monkeypatch, cli):
    printed: list = []

    class _Evaluator:
        def __init__(self, run_llm):
            assert run_llm is True

        def run(self):
            return "the report"

    monkeypatch.setattr(main, "AgentEvaluator", _Evaluator)
    monkeypatch.setattr(main, "print_eval_report", printed.append)

    cli("--mode", "eval")

    assert printed == ["the report"]


# ── The shared pipeline entry, the reconciler thread, the shutdown handler ──────────────────────────

def test_the_cli_pipeline_runs_through_the_one_shared_pipeline(monkeypatch):
    seen: dict = {}

    async def _pipeline(events, label, correlation_id):
        seen.update(events=events, label=label, cid=correlation_id)
        return {"ok": 1}

    monkeypatch.setattr(main, "run_pipeline", _pipeline)
    monkeypatch.setattr(main, "new_correlation_id", lambda: "cid")

    assert main._run_pipeline(["e1"], "LABEL") == {"ok": 1}
    assert seen == {"events": ["e1"], "label": "LABEL", "cid": "cid"}


def test_the_reconciler_keeps_going_after_a_failed_pass_and_stops_on_shutdown(monkeypatch):
    import src.swim_file_ingest as ingest
    import threading
    import time

    threads: list = []

    class _Thread:
        def __init__(self, target, daemon, name):
            self.target, self.daemon, self.name = target, daemon, name
            threads.append(self)

        def start(self):
            pass

    passes: list[str] = []

    def _run_async(coro):
        coro.close()
        passes.append("pass")
        if len(passes) == 1:
            raise RuntimeError("database down")

    asked = iter([False, False, True])
    monkeypatch.setattr(threading, "Thread", _Thread)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(ingest, "shutdown_requested", lambda: next(asked))
    monkeypatch.setattr(main, "run_async", _run_async)

    main._start_reconciler()
    [thread] = threads
    assert (thread.daemon, thread.name) == (True, "reconciler")
    thread.target()

    assert passes == ["pass", "pass"], "the first pass failed, the second still ran, then shutdown ended the loop"


def test_a_shutdown_signal_asks_the_scan_loop_to_finish_its_file(monkeypatch):
    installed: dict = {}
    monkeypatch.setattr(main.signal, "signal", lambda sig, handler: installed.update({sig: handler}))
    asked: list = []
    monkeypatch.setattr(main, "request_shutdown", lambda: asked.append(1))

    main._install_shutdown_handler()

    assert set(installed) == {signal.SIGTERM, signal.SIGINT}
    installed[signal.SIGTERM](signal.SIGTERM, None)
    assert asked == [1]
