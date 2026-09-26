# instraclustr: the infrastructure stack

The shared services the application stack in [`../nas-feedops-sentinel`](../nas-feedops-sentinel) runs on. [`../launch_nas_sentinel.ps1`](../launch_nas_sentinel.ps1) starts this stack first on the external `nas-network`, waits for it to report healthy, checks that its bootstrap jobs succeeded, and only then starts the application. Every published port is bound to `127.0.0.1`.

| Service | Role |
|---|---|
| `postgres` | the application database |
| `pgbouncer` | transaction pooling in front of Postgres; application services connect through it |
| `vault` | secrets, on persistent Raft storage (`vault-config.hcl`) |
| `vault-unseal` (job) | `vault-unseal-and-seed.bash`, on every start: unseals Vault with 2 of its 3 key shares (initializing it after a `down -v`), seeds secrets, and reconciles the per-service AppRoles and their credentials |
| `litellm` | the model gateway, configured by `litellm-config.yaml` |
| `litellm-db-init` (job) | `litellm-db-init.sh`: creates LiteLLM's own `litellm` database |
| `litellm-bootstrap` (job) | `litellm-bootstrap.sh`: issues the application's scoped LiteLLM key and stores it in Vault |

How the identities, key shares and recovery work is in [docs/operations.md](../nas-feedops-sentinel/docs/operations.md). Share 3 of the unseal key is written to `vault-recovery/`; move it off the machine.

## Configuration

Copy `.env.example` to `.env`; it is gitignored. The stack will not start without `POSTGRES_PASSWORD`, `APP_DB_PASSWORD`, `LITELLM_MASTER_KEY` and `LITELLM_DB_PASSWORD`. `APP_DB_PASSWORD` must match the value in `../nas-feedops-sentinel/.env`, because the seed job copies it into Vault for the application's database role. Provider keys (`DEEPSEEK_API_KEY`, `GEMINI_API_KEY`, and `GEMINI_API_KEY_2` or `_3` if used) belong here, since only the gateway needs them.

## Models

The application names aliases only. `litellm-config.yaml` decides which provider serves each one:

- `primary-model` is DeepSeek, falling back to `gemini-backup` on outages, timeouts and rate limits.
- `gemini-backup` has one deployment per Gemini key, so the proxy spreads calls and benches a key that hits its quota.

Pointing an alias at Claude, OpenAI or another Gemini model is an edit to that file; nothing in the application changes. The application's key can call only `primary-model` and `gemini-backup`, within `LITELLM_APP_BUDGET_USD` per 30 days and `LITELLM_APP_RPM` requests per minute.

## Retired files

This folder once ran a Cassandra ↔ Kafka ↔ Elasticsearch pipeline. It was never wired into the application and was removed in 17d578c; its services survive only as commented-out blocks in `docker-compose.yml`. These files belonged to it, or were superseded, and nothing uses them:

- `Dockerfile.connect`, `cassandra-init.sh`
- `cassandra-sink-connector.json`, `cassandra-source-connector.json`, `elasticsearch-sink-connector.json`, `elasticsearch-confluent-sink.json`, `sink-config.json`
- `deploy-connector.ps1`, `deploy-connectors.sh`, `deploy-elasticsearch-sink.sh`
- `test-end-to-end.sh`, `test-pipeline.ps1`, `test-pipeline.sh`, `verify-pipeline.bat`, `verify-pipeline.ps1`
- `pipeline-test-events.jsonl`, `test-events.jsonl`
- `scripts/vault-init.sh`, superseded by `vault-unseal-and-seed.bash`
