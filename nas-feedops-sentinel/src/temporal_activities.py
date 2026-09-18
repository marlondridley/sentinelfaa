"""Temporal activity definitions."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from temporalio import activity

from src.logging_config import bind_correlation_id, get_logger
from src.models import NASEvent
from src.pipeline import run_pipeline
from src.swim_file_ingest import scan_swim_folder

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
    events, stats = scan_swim_folder(source)
    summary = await run_pipeline(events, label="INGEST", correlation_id=cid)
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