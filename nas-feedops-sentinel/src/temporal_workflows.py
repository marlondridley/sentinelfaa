"""Temporal workflow definitions."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from src.temporal_activities import ingest_folder_activity, process_events_activity

# ---------------------------------------------------------------------------
# Failure semantics
#
# Previously both activities were scheduled with only a start_to_close_timeout,
# so they inherited Temporal's default retry policy: effectively unlimited
# attempts with backoff. That is the wrong default here, because the most
# likely failure in this pipeline is a *permanent* one -- a malformed event
# payload that fails NASEvent.model_validate(). Retrying a structurally
# invalid payload produces the identical failure forever, burning worker slots
# and burying the real cause in retry noise.
#
# So: retry the things that can plausibly succeed on a second attempt
# (database blip, LiteLLM timeout, transient volume mount), and fail fast on
# the things that cannot (schema/type violations).
#
# Deliberately NOT set: heartbeat_timeout. Both activities are a single long
# await with no inner loop, so nothing calls activity.heartbeat(); declaring a
# heartbeat timeout without emitting heartbeats would just manufacture
# spurious activity timeouts. Adding real heartbeats would require chunking
# the folder scan, which is a separate change.
# ---------------------------------------------------------------------------

# Exception class names, matched by Temporal as strings at failure time.
# pydantic raises ValidationError; ValueError/TypeError cover hand-rolled
# payload guards in the pipeline.
_PERMANENT_DATA_ERRORS = [
    "ValidationError",
    "ValueError",
    "TypeError",
]

_PIPELINE_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=5,
    non_retryable_error_types=_PERMANENT_DATA_ERRORS,
)

# The ingest side additionally touches the filesystem. A missing directory is
# left retryable on purpose: during container start the ./data bind mount can
# briefly not be there yet, and that case does resolve on its own.
_INGEST_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=5,
    non_retryable_error_types=_PERMANENT_DATA_ERRORS,
)


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
            retry_policy=_PIPELINE_RETRY_POLICY,
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
            retry_policy=_INGEST_RETRY_POLICY,
        )
