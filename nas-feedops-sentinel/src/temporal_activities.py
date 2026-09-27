"""Temporal activity definitions."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from temporalio import activity

from src import config
from src.logging_config import bind_correlation_id, get_logger
from src.models import NASEvent
from src.pipeline import run_pipeline
from src.swim_file_ingest import commit_scan, scan_swim_folder

_log = get_logger(__name__)


@activity.defn
async def ingest_folder_activity(
    source_dir: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Parse files from the ingest directory and run the pipeline."""
    cid = bind_correlation_id(correlation_id)

    # scan_swim_folder resolves None to config.DATA_DIR / "raw" / "swim_live"
    # itself (see src/swim_file_ingest.py) -- the same default main.py's
    # replay mode and the dashboard rely on -- so we don't duplicate that
    # default here.
    source = Path(source_dir) if source_dir else None
    if source is not None:
        # Workflows can be started directly on Temporal, not only through the
        # API, so the activity itself refuses paths outside the raw-data root:
        # scan_swim_folder() moves every file it finds.
        root = (config.DATA_DIR / "raw").resolve()
        if not source.resolve().is_relative_to(root):
            raise ValueError(f"source_dir must be inside {root}")

    # scan_swim_folder() is synchronous and reaches storage through
    # src/async_bridge.run_async(), which drives its own long-lived event loop
    # with run_until_complete(). Called directly from here -- inside the
    # Temporal worker's running loop -- that raised "Cannot run the event
    # loop while another loop is running" on the first file, and because
    # RuntimeError is not in _PERMANENT_DATA_ERRORS the workflow retried it
    # to exhaustion. A worker thread has no running loop, so the bridge works
    # there; it also keeps blocking file I/O off the worker's loop, which
    # Temporal requires of async activities anyway. (The storage pool is
    # per-loop -- see src/storage/pool.py -- so the scan's writes on the
    # bridge loop and run_pipeline's on this one no longer collide.)
    #
    # The scan defers archiving: the files are archived and logged only once the
    # pipeline has stored their events. If it fails they stay claimed, Temporal
    # retries the activity, and the claims are released for another scan.
    events, stats = await asyncio.to_thread(scan_swim_folder, source, defer_archive=True)
    summary = await run_pipeline(events, label="INGEST", correlation_id=cid)
    await asyncio.to_thread(commit_scan, stats)
    _log.info(
        "ingest_folder_complete",
        extra={"stats": stats.as_dict(), "pipeline": summary},
    )
    return {"stats": stats.as_dict(), "pipeline": summary}


@activity.defn
async def process_events_activity(
    event_payloads: list[dict[str, Any]],
    label: str = "PIPELINE",
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Hydrate NASEvent payloads and run the pipeline."""
    cid = bind_correlation_id(correlation_id)
    events = [NASEvent.model_validate(p) for p in event_payloads]
    return await run_pipeline(events, label=label, correlation_id=cid)