# HTTP API — deployment wiring and docs

Drop-in snippets for the two files not included in this working set. Paste the compose
service into `docker-compose.yml` and the README section into `README.md`.

## 1. docker-compose.yml

Add alongside the existing worker service. The image is the same; only the command differs.

```yaml
  api:
    build: .
    command: uvicorn src.api:app --host 0.0.0.0 --port 8000
    ports:
      - "8000:8000"
    environment:
      - API_KEY=${API_KEY}
      - TEMPORAL_HOST=${TEMPORAL_HOST:-temporal:7233}
      - TEMPORAL_TASK_QUEUE=${TEMPORAL_TASK_QUEUE}
      - CORS_ALLOWED_ORIGINS=${CORS_ALLOWED_ORIGINS}
    depends_on:
      - temporal
    healthcheck:
      # Liveness. Readiness (which probes storage) is GET /v1/ready.
      test: ["CMD", "python", "-c",
             "import urllib.request;urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
```

`uvicorn` must be in `requirements.txt` / `pyproject.toml` — it is a transitive dev
dependency of FastAPI in some setups but is not guaranteed at runtime:

```
uvicorn[standard]>=0.30
```

The Dockerfile `CMD` stays as-is (`python -m src.main --mode swim-folder --metrics`) —
that is the worker entrypoint, and this compose service overrides it for the API only.

## 2. README section

````markdown
### HTTP API

The FastAPI app (`src/api.py`) triggers Temporal workflows over HTTP. It runs as its own
process, separate from the swim-folder worker started by the default Dockerfile `CMD`.

Run it:

```bash
docker compose up api
# or, locally:
uvicorn src.api:app --host 0.0.0.0 --port 8000
```

Base URL: `http://localhost:8000`. Interactive spec at `/docs`, raw OpenAPI at
`/openapi.json`. Application routes are namespaced under `/v1`.

| Method | Path          | Auth | Purpose                                               |
| ------ | ------------- | ---- | ----------------------------------------------------- |
| GET    | `/health`     | no   | Liveness. Probes no dependencies.                      |
| GET    | `/v1/health`  | no   | Versioned alias of `/health`.                          |
| GET    | `/v1/ready`   | no   | Readiness. Probes storage; 503 when a check fails.     |
| POST   | `/v1/ingest`  | yes  | Start a pipeline or ingest workflow.                   |

Note: the worker's Prometheus server on port 9108 also exposes `/health`, `/ready`, and
`/metrics`. Those are the *worker's* probes and are unrelated to the API on port 8000.

Auth: `X-API-Key: <key>` or `Authorization: Bearer <key>`, matched against `API_KEY`
(falling back to `LITELLM_API_KEY`). If neither is set, auth is disabled — do not run
that way outside local development.

**Liveness / readiness**

```bash
curl http://localhost:8000/health
# {"status":"ok","task_queue":"sentinel-tq","correlation_id":"..."}

curl -i http://localhost:8000/v1/ready
# 200 {"status":"ready","checks":{"storage":"ok"},...}
# 503 {"status":"not_ready","checks":{"storage":"error: ..."},...}
```

**Ingest inline events**

```bash
curl -X POST http://localhost:8000/v1/ingest \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: batch-2026-09-16-0001" \
  -H "x-correlation-id: my-trace-id" \
  -d '{
        "label": "manual-backfill",
        "events": [
          {"event_time_utc": "2026-09-16T14:02:11", "airport": "IAD", "service_name": "TAIS"}
        ]
      }'
# {"workflow_id":"sentinel-pipeline-batch-2026-09-16-0001",
#  "workflow_key":"batch-2026-09-16-0001","correlation_id":"my-trace-id",
#  "task_queue":"sentinel-tq"}
```

**Ingest from a directory** — used only when `events` is empty:

```bash
curl -X POST http://localhost:8000/v1/ingest \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"source_dir": "data/raw/swim"}'
```

**Idempotency.** Supply `Idempotency-Key` so a retry after a timeout attaches to the
existing workflow instead of reprocessing the batch. Without the header, an inline-events
request derives the key from a hash of the payload; a `source_dir` request does not (each
scan is treated as a distinct run).

**Correlation.** Send `x-correlation-id` to thread a trace through logs; it is echoed on
every response. When an idempotency key is in play the workflow is named after the key,
and the `ingest_workflow_key_resolved` log line joins the two.

**Errors.**

| Status | Meaning                                                              |
| ------ | -------------------------------------------------------------------- |
| 401    | Missing or wrong API key.                                             |
| 422    | One or more `events` failed NASEvent validation; body lists per-item field errors, indexed into the submitted array. No workflow was started. |
| 503    | Temporal unreachable; no workflow was started. Safe to retry with the same idempotency key. |
````

## 3. If the API is not meant to ship

If `api.py` is experimental rather than a deliverable, delete it along with its
`start_ingest_workflow` / `start_pipeline_workflow` import surface rather than leaving an
undocumented entrypoint in the tree.
