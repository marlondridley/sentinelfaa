# HTTP API

The FastAPI service (`src/api.py`) starts Temporal workflows over HTTP. It runs as the `api` service in `docker-compose.yml`, alongside the `temporal-worker` that executes the workflows. Its specification is committed as [openapi.json](openapi.json), generated from the application.

## Where it listens

The container serves on port 8000 inside `nas-network`. Only the `edge` proxy is published, so from the host the API is at **https://localhost:8444**. The proxy does not route `/docs` or `/metrics`, and `/docs` is off anyway unless `API_DOCS_ENABLED=true`.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | no | Liveness. Probes no dependencies. |
| GET | `/v1/health` | no | Versioned alias of `/health`. |
| GET | `/v1/ready` | no | Readiness. Reads from storage; 503 names the failing dependency. |
| POST | `/v1/ingest` | yes | Start a pipeline workflow (inline events) or a folder scan. |

## Authentication

Send `X-API-Key: <key>` or `Authorization: Bearer <key>`. Keys are compared in constant time against the named keys in `API_KEYS` (a JSON object of `{"caller-name": "key"}`, so each caller gets its own identity in the logs) and then `API_KEY`. The API fails closed: if neither is set, every call to `/v1/ingest` is refused with 503. The LiteLLM proxy key is never accepted as an API credential.

## POST /v1/ingest

**Inline events** start a pipeline workflow. Each event is validated against the `NASEvent` schema before anything starts, unknown fields are refused, and the server tags every event `data_origin=api` regardless of what the client sends.

```bash
curl -X POST https://localhost:8444/v1/ingest \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: batch-2026-09-16-0001" \
  -H "x-correlation-id: my-trace-id" \
  -d '{
        "label": "manual-backfill",
        "events": [{
          "airport_code": "KIAD", "tracon": "PCT", "service_name": "TERMINAL_AUTO",
          "message_type": "TAIS_FP",
          "event_time_utc": "2026-09-16T14:02:11Z",
          "received_time_utc": "2026-09-16T14:02:12Z"
        }]
      }'
```

```json
{"workflow_id": "sentinel-pipeline-batch-2026-09-16-0001",
 "workflow_key": "batch-2026-09-16-0001", "correlation_id": "my-trace-id",
 "task_queue": "nas-feedops-queue", "principal_id": "legacy_api_key"}
```

`principal_id` is the caller's name from `API_KEYS`, or `legacy_api_key` for the single `API_KEY`.

**An empty body** (`{}`) starts a scan of the server's own SWIM folder (`data/raw/swim_live`). The folder is server configuration: `source_dir` is no longer a request field, and sending it is refused with 422.

**Idempotency.** With an `Idempotency-Key` header, a retry after a timeout attaches to the existing workflow instead of processing the batch again. Without one, inline events derive the key from a hash of the payload; folder scans do not, since each scan is a distinct run. The key names the workflow, so it is 1 to 128 characters from letters, digits, `.`, `_`, `:` and `-`; anything else is refused with 422 `invalid_idempotency_key`, and nothing starts.

**Correlation.** Send `x-correlation-id` to thread a trace through the logs; it is echoed on every response. When an idempotency key names the workflow, the `ingest_workflow_key_resolved` log line joins the two. A folder scan with no idempotency key is named after it, so it is 1 to 64 characters from the same set (a UUID or a W3C trace id fits). A malformed one is not refused: it is replaced with a new id, which is the one echoed back, and the replacement is logged by length.

## Errors

Error bodies never echo exception text.

| Status | Meaning |
|---|---|
| 401 | Missing or wrong API key. |
| 422 | The body has an unknown field, or one or more `events` failed `NASEvent` validation. The body lists per-item field errors, indexed into the submitted array. No workflow was started. |
| 503 | No API key is configured, or Temporal is unreachable. No workflow was started; a retry with the same idempotency key is safe. |

## Regenerating openapi.json

```bash
API_DOCS_ENABLED=true python -c "import json; from src.api import app; \
open('openapi.json', 'w').write(json.dumps(app.openapi(), indent=2) + '\n')"
```
