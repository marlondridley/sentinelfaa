"""Temporal workflow definitions."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from src.temporal_activities import ingest_folder_activity, process_events_activity


@workflow.defn
class SentinelPipelineWorkflow:
    """Validate, alert, and evaluate a batch of NAS events."""

    @workflow.run
    async def run(
        self,
        event_payloads: list[dict[str, Any]],
        label: str = "PIPELINE",
        correlation_id: str = "",
    ) -> dict[str, Any]:
        return await workflow.execute_activity(
            process_events_activity,
            args=[event_payloads, label, correlation_id],
            start_to_close_timeout=timedelta(minutes=10),
        )


@workflow.defn
class IngestFolderWorkflow:
    """Replace legacy folder-watch / JMS consumers with a Temporal ingest run."""

    @workflow.run
    async def run(
        self,
        source_dir: str | None = None,
        correlation_id: str = "",
    ) -> dict[str, Any]:
        return await workflow.execute_activity(
            ingest_folder_activity,
            args=[source_dir, correlation_id],
            start_to_close_timeout=timedelta(minutes=15),
        )
