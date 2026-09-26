"""HTTP API with x-correlation-id middleware, CORS, and auth gates. Starts Temporal workflows."""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import make_asgi_app
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src import storage
from src.auth import Principal, PrincipalSource, principal_from_api_key
from src.config import settings
from src.logging_config import (
    CORRELATION_HEADER,
    bind_correlation_id,
    configure_logging,
    get_correlation_id,
    get_logger,
)
from src.models import NASEvent
from temporalio.service import RPCError

from src.temporal_worker import get_or_create_client, start_ingest_workflow, start_pipeline_workflow

configure_logging()
_log = get_logger(__name__)

# `version` is the source of truth for the /v1 route prefix below: bump it, and the
# OpenAPI metadata, alongside any breaking change to IngestRequest or the /ingest
# response shape.
# Interactive docs only when explicitly enabled; the schema is still in openapi.json.
app = FastAPI(
    title="nas-feedops-sentinel",
    version="1.0.0",
    docs_url="/docs" if settings.API_DOCS_ENABLED else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.API_DOCS_ENABLED else None,
)

# Routes live under /v1 so a future contract change can be served alongside the old
# shape. /health is additionally mounted unprefixed for orchestrator probes that are
# already pointed at it.
router = APIRouter(prefix="/v1")

IDEMPOTENCY_HEADER = "Idempotency-Key"

# ── CORS Middleware ───────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Authorization", "Content-Type", IDEMPOTENCY_HEADER, CORRELATION_HEADER],
)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_auth = HTTPBearer(auto_error=False)


async def verify_auth(
    api_key: str | None = Security(api_key_header),
    bearer: HTTPAuthorizationCredentials | None = Security(bearer_auth),
) -> Principal:
    """
    Enforce authentication for privileged control/ingestion endpoints and resolve the
    caller's Principal.

    Accepts X-API-Key header or Authorization: Bearer <key>, checked against the named
    API_KEYS map first (so distinct callers get distinct, auditable identities) and the
    legacy single API_KEY/LITELLM_API_KEY second (recorded as the generic
    "legacy_api_key" principal).

    If no key is configured at all, this fails open (unchanged prior behavior, meant for
    local/dev deployments) but returns an explicit "anonymous" Principal rather than
    silently returning nothing, so callers of this dependency always get an identity to
    log or attribute against -- the audit trail can distinguish "ran with auth
    configured" from "ran with no auth configured" instead of conflating the two.
    """
    # API_KEY / API_KEYS only. This used to fall back to LITELLM_API_KEY, so
    # the LLM proxy credential -- now the proxy master key -- doubled as the
    # ingest API credential.
    expected_key = settings.API_KEY
    named_keys = settings.API_KEYS
    provided = api_key or (bearer.credentials if bearer else None)

    if not expected_key and not named_keys:
        if settings.API_AUTH_REQUIRED:
            _log.error("api_auth_not_configured", extra={"correlation_id": get_correlation_id()})
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="API authentication is not configured; set API_KEY or API_KEYS.",
            )
        _log.warning("api_auth_disabled", extra={"correlation_id": get_correlation_id()})
        return Principal(principal_id="anonymous", role="unauthenticated", source=PrincipalSource.ANONYMOUS)

    principal = principal_from_api_key(provided or "", named_keys=named_keys, legacy_key=expected_key)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


class IngestRequest(BaseModel):
    # Unknown fields are refused. source_dir used to be accepted and handed to
    # scan_swim_folder(), which MOVES every file it finds -- an authenticated
    # caller could point the worker at any directory it can write (/app/src
    # included). Ingest folders are server-side configuration now.
    model_config = ConfigDict(extra="forbid")

    events: list[dict[str, Any]] = Field(
        default=[],
        max_length=10000,
        description=(
            "Inline event payloads to ingest directly. If this list is non-empty, "
            "a pipeline workflow is started from these events; if it is empty, the "
            "server scans its own SWIM folder instead. Each entry is validated against the NASEvent "
            "schema synchronously; malformed entries return 422 before any workflow starts. "
            "Capped at 10,000 entries per request so a single call can't block the "
            "request-handling event loop or exhaust memory."
        ),
    )
    label: str = Field(
        default="API",
        description="Label attached to the pipeline workflow run, used for tracing/identification in Temporal.",
    )


def _validate_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Validate each raw event dict against NASEvent at the HTTP boundary.

    Raises HTTPException(422) with per-item field errors so the caller learns about a
    malformed payload in the response, rather than only in worker logs after a 200.
    """
    failures: list[dict[str, Any]] = []
    for index, payload in enumerate(events):
        try:
            NASEvent.model_validate(payload)
        except ValidationError as exc:
            failures.append({"index": index, "errors": exc.errors()})

    if failures:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "event_validation_failed",
                "invalid_count": len(failures),
                "total_count": len(events),
                "failures": failures,
            },
        )
    return events


def _idempotency_key(supplied: str | None, events: list[dict[str, Any]]) -> str | None:
    """
    Resolve the key that seeds the Temporal workflow id.

    An explicit Idempotency-Key header always wins. Absent one, an inline-events batch
    falls back to a hash of the canonical payload, so a client retry after a timeout
    attaches to the existing workflow instead of starting a second run over the same
    batch. Returns None for a folder scan with no explicit key, where repeated
    scans of the same directory are legitimately distinct runs.
    """
    if supplied:
        return supplied.strip()
    if not events:
        return None
    canonical = json.dumps(events, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


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
    """
    Liveness only: confirm the API process is up and serving.

    Deliberately probes no dependencies — a dependency outage should not cause an
    orchestrator to restart an otherwise healthy process. Use /v1/ready for that.
    """
    return {
        "status": "ok",
        "task_queue": settings.TEMPORAL_TASK_QUEUE,
        "correlation_id": get_correlation_id(),
    }


@router.get("/health", summary="Health check", description="Liveness check for the API process.")
async def health_v1() -> dict[str, str]:
    """Versioned alias of /health. See health() for semantics."""
    return await health()


READINESS_TIMEOUT_SECONDS = 5.0


def model_proxy_readiness_url(proxy_url: str) -> str:
    # LiteLLM's /health needs the master key and calls every configured model;
    # /health/readiness is unauthenticated and checks only the proxy and its DB.
    parts = urlsplit(proxy_url)
    return urlunsplit((parts.scheme, parts.netloc, "/health/readiness", "", ""))


async def _check_storage() -> None:
    await storage.get_recent_events(limit=1)


async def _check_temporal() -> None:
    client = await get_or_create_client()
    if not await client.service_client.check_health():
        raise RuntimeError("Temporal WorkflowService reports unavailable")


async def _check_model_proxy() -> None:
    async with httpx.AsyncClient() as http:
        (await http.get(model_proxy_readiness_url(settings.LITELLM_PROXY_URL))).raise_for_status()


_READINESS_CHECKS = {
    "storage": _check_storage,
    "temporal": _check_temporal,
    "model_proxy": _check_model_proxy,
}


@router.get(
    "/ready",
    summary="Readiness check",
    description=(
        "Readiness probe: confirms storage, the Temporal server and the model proxy are "
        "reachable. Returns 503 naming each failing dependency."
    ),
)
async def ready() -> JSONResponse:
    """
    Readiness: probe the three dependencies /ingest needs before accepting traffic.

    Storage (Postgres via PgBouncer), the Temporal server and the model proxy are
    probed concurrently, each bounded by READINESS_TIMEOUT_SECONDS. Returns 503 and
    names each failing dependency so an orchestrator can pull the instance out of
    rotation without killing the process.
    """
    names = list(_READINESS_CHECKS)
    outcomes = await asyncio.gather(
        *(asyncio.wait_for(check(), READINESS_TIMEOUT_SECONDS) for check in _READINESS_CHECKS.values()),
        return_exceptions=True,
    )
    checks: dict[str, str] = {}
    for name, outcome in zip(names, outcomes):
        if isinstance(outcome, BaseException):
            checks[name] = "error"
            _log.warning("readiness_check_failed", extra={"dependency": name, "error": repr(outcome)})
        else:
            checks[name] = "ok"
    healthy = all(state == "ok" for state in checks.values())

    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ready" if healthy else "not_ready",
            "checks": checks,
            "correlation_id": get_correlation_id(),
        },
    )


@router.post(
    "/ingest",
    summary="Start an ingestion workflow",
    description=(
        "Starts a Temporal workflow to process NAS events. If `events` is provided, a "
        "pipeline workflow runs directly on those events; otherwise an ingest workflow scans "
        "the server's SWIM folder (data/raw/swim_live). Inline events are validated against the NASEvent schema before "
        "the workflow starts; invalid payloads return 422. Supply an `Idempotency-Key` header "
        "to make retries attach to the existing workflow. Requires authentication via "
        "`X-API-Key` or a Bearer token; the resolved caller identity is recorded against "
        "this call in the application logs."
    ),
)
async def ingest(
    body: IngestRequest,
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    principal: Principal = Depends(verify_auth),
) -> JSONResponse:
    """
    Start a Temporal workflow to ingest and process NAS events.

    Branching behavior: if `events` is non-empty, a pipeline workflow is started from
    those events; otherwise an ingest workflow scans the server's configured SWIM
    folder. The folder is server-side configuration, never a request field.

    Inline events are validated against NASEvent synchronously, so a malformed batch is
    rejected with 422 here rather than failing later inside process_events_activity where
    the caller cannot see it.

    Retries: the workflow id is seeded from the Idempotency-Key header, falling back to a
    hash of the inline events payload, so a retried POST re-attaches to the in-flight
    workflow instead of double-processing the batch.
    """
    cid = get_correlation_id()

    _log.info(
        "ingest_request_authenticated",
        extra={
            "correlation_id": cid,
            "principal_id": principal.principal_id,
            "principal_source": principal.source.value,
        },
    )

    if body.events:
        # Provenance is the server's to assign: a client cannot label what it
        # posts as the live feed.
        body.events = [{**payload, "data_origin": "api"} for payload in body.events]
        _validate_events(body.events)

    workflow_key = _idempotency_key(idempotency_key, body.events) or cid
    if workflow_key != cid:
        # Keep the request cid discoverable in logs — the workflow is named after
        # workflow_key, so this line is the join between the two.
        _log.info(
            "ingest_workflow_key_resolved",
            extra={
                "correlation_id": cid,
                "workflow_key": workflow_key,
                "explicit_key": bool(idempotency_key),
            },
        )

    try:
        if body.events:
            workflow_id = await start_pipeline_workflow(
                body.events, label=body.label, correlation_id=workflow_key
            )
        else:
            workflow_id = await start_ingest_workflow(correlation_id=workflow_key)
    except RPCError as exc:
        # Temporal server unreachable/timed out -- an expected, transient
        # infrastructure failure. Anything else (a real bug in workflow
        # construction, a programming error) is intentionally left to
        # propagate to FastAPI's top-level handler rather than being
        # mislabeled as "temporal_unavailable" here.
        _log.error("ingest_workflow_unavailable", extra={"error": str(exc)})
        return JSONResponse(
            status_code=503,
            content={"error": "temporal_unavailable", "correlation_id": cid},
        )
    return JSONResponse(
        {
            "workflow_id": workflow_id,
            "workflow_key": workflow_key,
            "correlation_id": cid,
            "task_queue": settings.TEMPORAL_TASK_QUEUE,
            "principal_id": principal.principal_id,
        }
    )


app.include_router(router)

# Prometheus scrape endpoint for the API process. Served on the internal
# network only: the edge proxy (docker/edge/nginx.conf) does not route it.
app.mount("/metrics", make_asgi_app())