"""HTTP API with x-correlation-id middleware. Starts Temporal workflows."""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.config import settings
from src.logging_config import (
    CORRELATION_HEADER,
    bind_correlation_id,
    configure_logging,
    get_correlation_id,
    get_logger,
)
from src.temporal_worker import start_ingest_workflow, start_pipeline_workflow

configure_logging()
_log = get_logger(__name__)

app = FastAPI(title="nas-feedops-sentinel", version="1.0.0")


class IngestRequest(BaseModel):
    events: list[dict[str, Any]] = []
    label: str = "API"
    source_dir: str | None = None


@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    cid = bind_correlation_id(request.headers.get(CORRELATION_HEADER))
    _log.info(
        "http_request",
        extra={"path": request.url.path, "method": request.method},
    )
    response = await call_next(request)
    response.headers[CORRELATION_HEADER] = cid
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "task_queue": settings.TEMPORAL_TASK_QUEUE,
        "correlation_id": get_correlation_id(),
    }


@app.post("/ingest")
async def ingest(body: IngestRequest) -> JSONResponse:
    cid = get_correlation_id()
    try:
        if body.events:
            workflow_id = await start_pipeline_workflow(
                body.events, label=body.label, correlation_id=cid
            )
        else:
            workflow_id = await start_ingest_workflow(
                source_dir=body.source_dir, correlation_id=cid
            )
    except Exception as exc:  # noqa: BLE001
        _log.error("ingest_workflow_failed", extra={"error": str(exc)})
        return JSONResponse(
            status_code=503,
            content={"error": "temporal_unavailable", "detail": str(exc), "correlation_id": cid},
        )
    return JSONResponse(
        {
            "workflow_id": workflow_id,
            "correlation_id": cid,
            "task_queue": settings.TEMPORAL_TASK_QUEUE,
        }
    )
