"""Temporal worker and client helpers."""
from __future__ import annotations

import asyncio
import signal
from datetime import timedelta

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


async def run_worker(interrupt_event: asyncio.Event) -> None:
    configure_logging()
    new_correlation_id()
    client = await connect_client()
    worker = Worker(
        client,
        task_queue=settings.TEMPORAL_TASK_QUEUE,
        workflows=[SentinelPipelineWorkflow, IngestFolderWorkflow],
        activities=[process_events_activity, ingest_folder_activity],
        # On shutdown, give in-flight activities (e.g. a file scan or pipeline
        # run already underway) this long to finish naturally before Temporal
        # sends them a cancellation. Keep this comfortably inside whatever
        # grace period the orchestrator gives the container on SIGTERM
        # (Docker/ECS/K8s default 30s) so the process still exits on time.
        graceful_shutdown_timeout=timedelta(seconds=20),
    )
    _log.info(
        "temporal_worker_started",
        extra={"task_queue": settings.TEMPORAL_TASK_QUEUE, "host": settings.TEMPORAL_HOST},
    )
    # `async with worker:` starts polling on enter and, on exit, calls
    # worker.shutdown() -- which is what actually drains in-flight activities
    # within graceful_shutdown_timeout instead of dropping them. Waiting on
    # interrupt_event (set by the SIGTERM/SIGINT handlers below) is what lets
    # us reach that exit deliberately rather than via an abrupt process kill.
    async with worker:
        _log.info("temporal_worker_polling")
        await interrupt_event.wait()
        _log.info("temporal_worker_shutdown_initiated")


def main() -> None:
    interrupt_event = asyncio.Event()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, interrupt_event.set)

    try:
        loop.run_until_complete(run_worker(interrupt_event))
    finally:
        _log.info("temporal_worker_stopped")
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


if __name__ == "__main__":
    main()