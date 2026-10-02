"""
The Temporal activities (src/temporal_activities.py): what runs when a pipeline or an ingest workflow reaches
the worker. The scan, the pipeline and the quarantine are replaced; these tests pin the order and the cleanup.
"""
from __future__ import annotations

import asyncio

import pytest

from src import temporal_activities as ta
from src.swim_file_ingest import IngestStats


class _Steps(list):
    stats: IngestStats


@pytest.fixture
def steps(monkeypatch):
    """Replace the scan, pipeline and cleanup with recorders; return the list they write to."""
    log = _Steps()
    stats = IngestStats()

    monkeypatch.setattr(ta, "scan_swim_folder", lambda source, defer_archive: log.append("scan") or (["e1"], stats))
    monkeypatch.setattr(ta, "commit_scan", lambda s: log.append("commit"))
    monkeypatch.setattr(ta, "release_scan", lambda s: log.append("release"))
    monkeypatch.setattr(ta, "run_retention", lambda: log.append("retention"))

    async def _pipeline(events, label, correlation_id):
        log.append(f"pipeline:{label}")
        return {"stored": len(events)}

    monkeypatch.setattr(ta, "run_pipeline", _pipeline)
    log.stats = stats
    return log


async def test_an_ingest_scans_runs_the_pipeline_then_commits_and_ages_out(steps):
    result = await ta.ingest_folder_activity()

    assert steps == ["scan", "pipeline:INGEST", "commit", "retention"]
    assert result["pipeline"] == {"stored": 1}
    assert result["stats"] == steps.stats.as_dict()


async def test_an_ingest_folder_outside_the_raw_data_root_is_refused_before_any_file_moves(steps, tmp_path):
    with pytest.raises(ValueError, match="source_dir must be inside"):
        await ta.ingest_folder_activity(str(tmp_path))

    assert steps == []


async def test_an_ingest_folder_inside_the_root_is_scanned(steps, monkeypatch, tmp_path):
    raw = tmp_path / "raw"
    (raw / "swim_live").mkdir(parents=True)
    monkeypatch.setattr(ta.config, "DATA_DIR", tmp_path)

    await ta.ingest_folder_activity(str(raw / "swim_live"))

    assert steps[0] == "scan"


async def test_a_cancelled_ingest_releases_its_claims_at_once_and_re_raises(steps, monkeypatch):
    async def _cancelled(events, label, correlation_id):
        raise asyncio.CancelledError

    monkeypatch.setattr(ta, "run_pipeline", _cancelled)

    with pytest.raises(asyncio.CancelledError):
        await ta.ingest_folder_activity()

    assert steps == ["scan", "release"], "released now, not after the claim timeout; nothing committed"


async def test_a_pipeline_failure_is_handed_to_the_quarantine_which_stores_file_by_file(steps, monkeypatch):
    attempts: list[str] = []

    async def _fails_then_stores(events, label, correlation_id):
        attempts.append(label)
        if len(attempts) == 1:
            raise RuntimeError("one file is poison")
        return {"stored": len(events)}

    def _isolate(events, stats, store, exc):
        steps.append(f"isolate:{type(exc).__name__}")
        assert store(["e1"]) == {"stored": 1}      # the store callback runs the same pipeline per file

    monkeypatch.setattr(ta, "run_pipeline", _fails_then_stores)
    monkeypatch.setattr(ta, "isolate_poison", _isolate)

    result = await ta.ingest_folder_activity()

    assert steps == ["scan", "isolate:RuntimeError", "commit", "retention"]
    assert result["pipeline"] == {"isolated_after": "RuntimeError"}


async def test_processing_events_validates_the_payloads_and_runs_the_pipeline(monkeypatch):
    from src.demo_scenario import generate_cascade_failure_scenario

    events = generate_cascade_failure_scenario()[:2]
    seen: dict = {}

    async def _pipeline(hydrated, label, correlation_id):
        seen.update(count=len(hydrated), label=label, types={type(e).__name__ for e in hydrated})
        return {"ok": True}

    monkeypatch.setattr(ta, "run_pipeline", _pipeline)

    result = await ta.process_events_activity([e.model_dump(mode="json") for e in events], label="REPLAY")

    assert result == {"ok": True}
    assert seen == {"count": 2, "label": "REPLAY", "types": {"NASEvent"}}
