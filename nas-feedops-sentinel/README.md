# nas-feedops-sentinel

The application: SWIM ingest, the alert and escalation pipeline, the investigation workflow, the API, the Temporal worker and the operator dashboard.

- What it is, its boundaries, and how to run the whole stack: the [repository README](../README.md)
- Identities, secrets, certificates, the live feed and alerting: [docs/operations.md](docs/operations.md)
- A scripted demonstration: [docs/walkthrough.md](docs/walkthrough.md)
- How AI is governed, boundary by boundary, with the Sentinel ↔ AGT crosswalk: [docs/agent-governance.md](docs/agent-governance.md)
- Where every agent metric comes from (AGT or in-repo, and why): [docs/agent-metrics.md](docs/agent-metrics.md)
- For operators: [docs/playbook.md](docs/playbook.md) · when something breaks: [docs/runbooks.md](docs/runbooks.md)
- The HTTP API: [API_DEPLOYMENT.md](API_DEPLOYMENT.md)

## Layout

```
src/
  swim_file_ingest.py     Jumpstart Kit folder: claim, parse, blockAtFAA, per-feed extractors,
                          stable ids, the watcher
  pipeline.py             the one pipeline every entry point runs
  validators.py  alert_engine.py  feed_health.py
  execution_gate.py       staging, tickets, and role-checked decisions
  reconcile.py            stages or EXPIRES any CRITICAL alert left unstaged
  investigation/          evidence, the structured record, four specialists, the workflow
  critic_agent.py  planner_agent.py  watch_officer_agent.py
  llm_client.py           the one LiteLLM client: redaction, policy, timeouts, error classes
  governance.py           AGT policy evaluation for model calls and tool calls; AGT audit log
  agent_tools.py          the agent tool boundary: tool registry, acting_as(), governed invoke()
  telemetry.py            OpenTelemetry: GenAI spans and metrics, AGT instruments, OTLP export
  model_telemetry.py      one model_calls row per call, projected from its chat span
  evaluation.py           the evaluation layer: per-investigation measures and gates, window metrics
  agt_metrics.py          AGT adapters (agent-sre, agent-os-kernel) and the in-repo exceptions
  readiness.py            the readiness verdict and deployment gate (python -m src.readiness)
  redaction.py  guardrails.py
  auth.py  keycloak_auth.py  Keycloak over HTTPS only
  storage/                asyncpg repositories, schema, migrations (migrate.py)
  api.py                  FastAPI: /health, /v1/ready, /v1/ingest
  temporal_worker.py  temporal_workflows.py  temporal_activities.py
  main.py                 the CLI
  config.py  vault_config.py  metrics.py  logging_config.py  devcert.py
dashboard/                app.py (Overview) and pages/: Live Airspace, Feed Health,
                          AI Critic & Review, Alert Log, Ingest & Replay, Agent Evaluation
                          (lib/agent_eval.py: the three layers of the agent evaluation)
policies/                 model-use.yaml and tool-use.yaml (AGT policies); model-slos.yaml
                          (readiness objectives and per-investigation gate limits)
tests/                    pytest suite; real SWIM captures in fixtures/swim_kit/;
                          test_agt_known_bugs.py documents AGT bugs as strict xfails
scripts/                  e2e_proof.py, Keycloak setup and RBAC, diagnostics
prompts/                  one YAML prompt per model-backed role
monitoring/  keycloak/  docker/  jumpstart/
```

## Command line

Run inside the application image (`docker compose run --rm --no-deps sentinel …`) or anywhere the database and Vault settings resolve:

```bash
python -m src.main --mode demo                  # labelled synthetic cascade failure, then one planner question
python -m src.main --mode sample                # a random synthetic batch of 50 events
python -m src.main --mode replay --path DIR     # scan a folder of captured messages once
python -m src.main --mode replay --watch        # the live watcher (what the sentinel service runs)
python -m src.main --mode eval                  # guardrail cases and deterministic scenarios
python -m src.reconcile                         # one reconciler pass
python -m src.readiness [--hours N] [--json] [--gate]   # model readiness verdict; --gate for CI/CD
python -m src.storage.migrate                   # schema, migrations and the app role (as schema owner)
```

Everything written by `demo`, `sample` and `replay` is tagged `synthetic` or `replay`, and the dashboard says so while it is on screen.

## Tests

Python 3.12. The suite needs a real Postgres, and it truncates every table before each test, so it refuses any database whose name does not contain `test`, `ci`, `scratch` or `tmp`:

```bash
pip install --require-hashes -r requirements-dev.lock
export POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5432 POSTGRES_DB=sentinel_test \
       POSTGRES_USER=... POSTGRES_PASSWORD=...
export DATABASE_URL= VAULT_ADDR= VAULT_TOKEN= CRITIC_ENABLED=0   # empty values are deliberate
python -m pytest
ruff check src tests scripts dashboard
```

Each empty value switches something off: `DATABASE_URL` so the `POSTGRES_*` settings are used, the Vault settings so Vault cannot redirect the connection to the live database, and `CRITIC_ENABLED` so no test calls a model. `src/storage/test_support.py` explains the guard. CI runs the same suite, plus `scripts/e2e_proof.py`, in the image's `test` stage against Postgres 16; `.github/workflows/pyapp-ci.yml` is the reference invocation.

## Dependencies

`requirements.txt` (runtime) and `requirements-dev.txt` (adds test tooling) are the ranges; `requirements.lock` and `requirements-dev.lock` are hash-locked and are what the image installs. Regenerating them is described at the top of `requirements.txt`. Accepted CVE exceptions must be recorded in [cve-allowlist.md](cve-allowlist.md); there are none.
