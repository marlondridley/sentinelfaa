"""Temporal worker and client helpers."""
from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from src.config import settings
from src.logging_config import configure_logging, get_correlation_id, get_logger, new_correlation_id
from src.temporal_activities import ingest_folder_activity, process_events_activity
from src.temporal_workflows import IngestFolderWorkflow, SentinelPipelineWorkflow

_log = get_logger(__name__)


async def connect_client() -> Client:
    return await Client.connect(
        settings.TEMPORAL_HOST,
        namespace=settings.TEMPORAL_NAMESPACE,
    )


async def start_pipeline_workflow(
    event_payloads: list[dict],
    label: str = "PIPELINE",
    correlation_id: str | None = None,
) -> str:
    cid = correlation_id or get_correlation_id()
    if not cid or cid == "-":
        cid = new_correlation_id()
    client = await connect_client()
    handle = await client.start_workflow(
        SentinelPipelineWorkflow.run,
        args=[event_payloads, label, cid],
        id=f"sentinel-pipeline-{cid}",
        task_queue=settings.TEMPORAL_TASK_QUEUE,
    )
    _log.info("workflow_started", extra={"workflow_id": handle.id, "label": label})
    return handle.id


async def start_ingest_workflow(
    source_dir: str | None = None,
    correlation_id: str | None = None,
) -> str:
    cid = correlation_id or get_correlation_id()
    if not cid or cid == "-":
        cid = new_correlation_id()
    client = await connect_client()
    handle = await client.start_workflow(
        IngestFolderWorkflow.run,
        args=[source_dir, cid],
        id=f"sentinel-ingest-{cid}",
        task_queue=settings.TEMPORAL_TASK_QUEUE,
    )
    _log.info("ingest_workflow_started", extra={"workflow_id": handle.id})
    return handle.id


async def run_worker() -> None:
    configure_logging()
    new_correlation_id()
    client = await connect_client()
    worker = Worker(
        client,
        task_queue=settings.TEMPORAL_TASK_QUEUE,
        workflows=[SentinelPipelineWorkflow, IngestFolderWorkflow],
        activities=[process_events_activity, ingest_folder_activity],
    )
    _log.info(
        "temporal_worker_started",
        extra={"task_queue": settings.TEMPORAL_TASK_QUEUE, "host": settings.TEMPORAL_HOST},
    )
    await worker.run()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
