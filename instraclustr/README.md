# instraclustr: the infrastructure stack

The shared services the application stack in [`../nas-feedops-sentinel`](../nas-feedops-sentinel) runs on. [`../launch_nas_sentinel.ps1`](../launch_nas_sentinel.ps1) starts this stack first on the external `nas-network`, waits for it to report healthy, checks that its bootstrap jobs succeeded, and only then starts the application. Every published port is bound to `127.0.0.1`.

| Service | Role |
|---|---|
| `postgres` | the application database |
| `pgbouncer` | transaction pooling in front of Postgres; application services connect through it |
| `vault` | secrets, on persistent Raft storage (`vault-config.hcl`); TLS only, with a certificate from the offline root (`certs/`) |
| `vault-unseal` (job) | `vault-unseal-and-seed.bash`, on every start: unseals Vault with 2 of its 3 key shares (initializing it after a `down -v`) and seeds the secrets this project owns, as the `vault-bootstrap` AppRole |
| `vault-ceremony` (on demand) | `terraform/ceremony.sh`: a root token from unseal shares 1 and 2, one `terraform plan` or `apply` of `terraform/`, then revoked |
| `spire-server` | workload identity: issues each application service its own X.509-SVID; its CA is signed by the offline root (`spire/server.conf`) |
| `spire-agent` | attests application sidecars through the Docker API and serves the Workload API socket they read (`spire/agent.conf`) |
| `litellm` | the model gateway, configured by `litellm-config.yaml` |
| `litellm-db-init` (job) | `litellm-db-init.sh`: creates LiteLLM's own `litellm` database |
| `litellm-bootstrap` (job) | `litellm-bootstrap.sh`: issues the application's scoped LiteLLM key and stores it in Vault |

How the identities, key shares and recovery work is in [docs/operations.md](../nas-feedops-sentinel/docs/operations.md). Share 3 of the unseal key is written to `vault-recovery/`; move it off the machine.

## Workload identity

No application container holds a credential, a Vault token, or a route to Vault. Each application service (`../nas-feedops-sentinel/docker-compose.yml`) has two sidecars:

1. **spiffe-helper**, labelled `com.sentinelfaa.identity=<service>`. spire-agent attests it through the Docker API and issues it `spiffe://sentinelfaa.internal/workload/<service>`: an X.509-SVID that lives one hour and is rotated at half-life.
2. **Vault Agent** logs in to Vault over mutual TLS with that SVID. Vault's cert auth ([`terraform/`](terraform/main.tf)) checks three things: the chain goes back to the offline root, the SPIFFE ID matches exactly, and the CN matches. Vault Agent then renders what the service's policy ([`vault-policies/`](vault-policies)) allows into a file on a memory-only volume. The service reads that file.

| What | Source of truth | Applied by | Drift check |
|---|---|---|---|
| Trust anchor, Vault TLS, SPIRE upstream CA | [`certs/policy/`](certs/policy) | the root ceremony ([`certs/README.md`](certs/README.md)) | `scripts/pki.ps1 check` (every launch) |
| Which container is which service | [`spire-entries.yaml`](spire-entries.yaml) | [`scripts/spire-entries-apply.py`](scripts/spire-entries-apply.py) | `spire-entries-apply.py --check` |
| What each service may read, and how it logs in | [`terraform/`](terraform/main.tf), [`vault-policies/`](vault-policies) | `vault-ceremony apply` | `vault-ceremony plan` (exit 2 = drift) |
| How sidecars fetch and render | [`vault-agent/`](vault-agent) | compose | CI |

`../scripts/smoke/02_negative_access_probe.sh` checks that every unauthorized path is refused:

- a container with no identity, or an unregistered one;
- a real SVID reaching for another service's secrets, policy, tokens or role;
- a missing or forged client certificate;
- plaintext HTTP, or TLS without the root.

## First-time setup

In this order (PowerShell, repository root):

1. **Certificates.** Run the root ceremony: [`certs/README.md`](certs/README.md).
2. **Infrastructure.** Start it, which initializes, unseals and seeds Vault:
   ```powershell
   docker compose -f instraclustr/docker-compose.yml --env-file instraclustr/.env up -d --build
   ```
   On the very first start spire-agent exits: it has no identity yet. That's expected.
3. **Vault's workload auth.** Run the root ceremony's Terraform step. `plan` shows the change; `apply` shows it again and asks for `yes`:
   ```powershell
   docker compose -f instraclustr/docker-compose.yml --env-file instraclustr/.env --profile ceremony run --rm vault-ceremony plan
   docker compose -f instraclustr/docker-compose.yml --env-file instraclustr/.env --profile ceremony run --rm vault-ceremony apply
   ```
4. **SPIRE: first boot.** This joins spire-agent with a single-use token that is never written to disk, then applies `spire-entries.yaml`. It needs Python 3 with PyYAML on this host.
   ```powershell
   .\instraclustr\scripts\spire-first-boot.ps1
   ```
5. **Everything else:** `.\launch_nas_sentinel.ps1`.

### SPIRE: first boot

spire-agent proves which host it runs on once, with a join token, and keeps the resulting identity in its `spire-agent-data` volume. Every later start (the launcher's included) reuses that identity and needs no token. Run `scripts/spire-first-boot.ps1` again only if that volume is deleted. The launcher stops with a pointer to it when spire-agent is not running.

The agent verifies spire-server against the offline root (`agent.conf`, `trust_bundle_path`) from its very first connection. There is no trust-on-first-use window.

### Changing what a service may read

Edit [`vault-policies/`](vault-policies) or [`terraform/main.tf`](terraform/main.tf), then run `vault-ceremony plan` and `apply` as in step 3. The bootstrap identity can't change policy by design, so every policy change needs the two unseal shares. Adding a service means entries in three places, which the tests keep in step:

- `spire-entries.yaml`;
- `terraform/main.tf`'s `workloads`;
- the application compose file (the service and its two sidecars).

### Threat model, stated plainly

- **Docker API access is root on this host.** Anyone with it can start a container carrying any service's identity label, read the unseal shares, or run the ceremony. Workload identity does not change that boundary, and could not on a single Docker host. What it does:
  - no credential exists at rest for the application;
  - each identity reaches only its own policy;
  - every Vault request is in Vault's audit log (`docker logs vault`) under the one service that made it, secret values HMAC'd;
  - a leaked certificate or rendered file is worthless off the host, or after an hour.
- **Probe 2 of the negative access probe** starts a container with the api label on purpose, and proves what that identity still cannot do.
- **Vault 1.15.2 is out of vendor support.** Upgrading it is a separate change. Vault Enterprise's FIPS 140-validated build is the route to SC-13 when a buyer requires it.

## Configuration

Copy `.env.example` to `.env`; it is gitignored. The stack will not start without `POSTGRES_PASSWORD`, `APP_DB_PASSWORD`, `LITELLM_MASTER_KEY` and `LITELLM_DB_PASSWORD`. The seed job copies the database passwords into Vault, the only place the application stack reads them from. `../nas-feedops-sentinel/.env` holds no database credential. Provider keys (`DEEPSEEK_API_KEY`, `GEMINI_API_KEY`, and `GEMINI_API_KEY_2` or `_3` if used) belong here, since only the gateway needs them.

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
