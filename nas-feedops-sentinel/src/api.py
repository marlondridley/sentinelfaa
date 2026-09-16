"""HTTP API with x-correlation-id middleware, CORS, and auth gates. Starts Temporal workflows."""
from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

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

# ── CORS Middleware ───────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_auth = HTTPBearer(auto_error=False)


async def verify_auth(
    api_key: str | None = Security(api_key_header),
    bearer: HTTPAuthorizationCredentials | None = Security(bearer_auth),
) -> None:
    """
    Enforce authentication for privileged control/ingestion endpoints.
    Accepts X-API-Key header or Authorization: Bearer <key>.
    """
    expected_key = settings.API_KEY or settings.LITELLM_API_KEY
    if not expected_key:
        return

    provided = api_key or (bearer.credentials if bearer else None)
    if not provided or provided != expected_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


class IngestRequest(BaseModel):
    events: list[dict[str, Any]] = Field(
        default=[],
        description=(
            "Inline event payloads to ingest directly. If this list is non-empty, "
            "it takes priority: a pipeline workflow is started from these events and "
            "`source_dir` is ignored."
        ),
    )
    label: str = Field(
        default="API",
        description="Label attached to the pipeline workflow run, used for tracing/identification in Temporal.",
    )
    source_dir: str | None = Field(
        default=None,
        description=(
            "Directory to ingest events from. Only used when `events` is empty; an ingest "
            "workflow is started against this directory instead of a pipeline workflow."
        ),
    )


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


@app.get("/health", summary="Health check", description="Liveness check for the API process.")
async def health() -> dict[str, str]:
    """Return service status, the active Temporal task queue, and the current request's correlation ID."""
    return {
        "status": "ok",
        "task_queue": settings.TEMPORAL_TASK_QUEUE,
        "correlation_id": get_correlation_id(),
    }


@app.post(
    "/ingest",
    dependencies=[Depends(verify_auth)],
    summary="Start an ingestion workflow",
    description=(
        "Starts a Temporal workflow to process NAS events. If `events` is provided, a "
        "pipeline workflow runs directly on those events; otherwise an ingest workflow runs "
        "against `source_dir`. Requires authentication via `X-API-Key` or a Bearer token."
    ),
)
async def ingest(body: IngestRequest) -> JSONResponse:
    """
    Start a Temporal workflow to ingest and process NAS events.

    Branching behavior: `body.events` takes priority over `body.source_dir` — if
    `events` is non-empty, a pipeline workflow is started from those events and
    `source_dir` is ignored; otherwise an ingest workflow is started against `source_dir`.
    """
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