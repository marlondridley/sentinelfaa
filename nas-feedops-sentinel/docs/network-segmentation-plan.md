# Network segmentation: the plan

Status: proposed, 2026-10-05. **Plan only: nothing in the stack, the compose files or the launcher has been changed.**
It describes how to stop every container from being able to reach every other container, which
[`../../instraclustr/README.md`](../../instraclustr/README.md) lists under "Not done here" and
[ADR 0003](adr/0003-spiffe-x509-mtls-for-workload-identity.md) lists as the final, watched cutover.

## In plain words

- **Today** every container, in both compose files, is attached to one Docker network called `nas-network`.
  No service declares a network of its own (`docker-compose.yml:748`, `../../instraclustr/docker-compose.yml:466`).
  That is what "flat" means: the API, Grafana, Keycloak, Vault and the databases can all open a connection to each other.
- **Segmenting** means defining several smaller networks and giving each container only the ones it needs.
  Two containers can talk only if they share a network, so a container that is broken into cannot even *try* to
  connect to Vault, Postgres or SPIRE unless it was meant to.
- **Why it matters here:** the services that guard the secrets and the data (Vault, SPIRE, the databases, the model
  gateway) are reachable today from the web-facing ones. The unauthenticated admin screens (Temporal UI,
  Alertmanager, Prometheus) are reachable from every container, and `/metrics` is open on all four application
  services (`instraclustr/tests/test_metrics_exposure.py`, added in PR 61, holds what keeps that acceptable today).
  It is the control assessors call boundary protection with deny by default (NIST 800-53 SC-7, SC-7(5), AC-4).
- **What it is not:** it does not encrypt anything. Postgres TLS and Temporal mTLS are separate, open items.

## How this was built

Read from the repository on 2026-10-05 (main at `7e80805`): both compose files in full, `monitoring/prometheus.yml`,
the Grafana datasource, `monitoring/alertmanager.yml`, `docker/edge/nginx.conf`, `instraclustr/spire/*.conf`,
`instraclustr/vault-agent/*.hcl`, the three one-shot scripts, and the application code that opens connections
(every HTTP client use in `src/`, `dashboard/` and `scripts/`, and the Temporal clients).

**Derived from reading, not yet observed on the running stack.** Phase 0 below closes that gap before anything is
changed. Where this plan relies on Docker behaviour I have not run on this machine, it says so and the rehearsal checks it.

## Who may talk to whom

Everything not on this list is refused. "Port" is what the service listens on inside the network.

| # | From | To | Port | Why | Read from |
|---|---|---|---|---|---|
| 1 | cloudflared (opt-in `public` profile) | edge | 8450, 8451, 8452 | the public tunnel's only destinations | compose 684-720, nginx.conf |
| 2 | edge | dashboard | 8501 | the web UI | nginx.conf 54 |
| 3 | edge | api | 8000 | `/health` and `/v1/` only | nginx.conf 65-68 |
| 4 | edge | grafana, temporal-ui, prometheus, alertmanager | 3000, 8080, 9090, 9093 | the admin screens, loopback only | nginx.conf 73-98 |
| 5 | edge | keycloak | 8443 (TLS) | the public sign-in pages | nginx.conf 143-165 |
| 6 | dashboard | keycloak | 8443 (TLS) | sign-in, server to server | `dashboard/lib/login.py:125`, `.streamlit/secrets.toml.example:73` |
| 7 | dashboard, sentinel, temporal-worker, api | pgbouncer | 6432 | the application database role; the API for its readiness check | compose 69 |
| 8 | pgbouncer | postgres | 5432 | the pool's own connection | infra compose 159 |
| 9 | migrate, proof | postgres | 5432 | schema owner, direct and not pooled | compose 157, 180 |
| 10 | litellm, litellm-db-init, postgres-scram | postgres | 5432 | LiteLLM's own database; the two one-shots | infra compose 382; `litellm-db-init.sh:12`, `postgres-scram.sh:13` |
| 11 | dashboard, sentinel, temporal-worker, proof | litellm | 4000 | model calls (the dashboard's Watch Officer brief, `0_Dashboard.py:381`) | compose 78 |
| 12 | api | litellm | 4000 | readiness check only (`/health/readiness`) | `src/api.py:348` |
| 13 | litellm-bootstrap | litellm | 4000 | mints the application's scoped key | `litellm-bootstrap.sh:18` |
| 14 | api, temporal-worker | temporal | 7233 | start and run workflows | `src/api.py:343`, `src/temporal_worker.py:33` |
| 15 | temporal-ui | temporal | 7233 | the UI's own backend | compose 497 |
| 16 | temporal | temporal-postgresql | 5432 | Temporal's own database | compose 481 |
| 17 | keycloak | keycloak-postgres | 5432 | Keycloak's own database | compose 409 |
| 18 | prometheus | api (`/metrics/`), sentinel, temporal-worker, dashboard, temporal, alertmanager | 8000, 9108, 9108, 9108, 8000, 9093 | scraping, over plain http | `prometheus.yml` |
| 19 | prometheus | alertmanager | 9093 | sends the alerts it fires | `prometheus.yml:17` |
| 20 | grafana | prometheus | 9090 | the dashboards' data | grafana datasource |
| 21 | the six vault-agent sidecars | vault | 8200 (mutual TLS) | each fetches its own service's secrets | `vault-agent/app.hcl:10`, `migrate.hcl:6` |
| 22 | vault-unseal, vault-ceremony, litellm-bootstrap | vault | 8200 (TLS) | unseal, policy changes, store the gateway key | infra compose 242, 274, 441 |
| 23 | spire-agent | spire-server | 8081 | node attestation and SVID issue | `spire/agent.conf:4` |

File names in the last column, in full: `nginx.conf` is `docker/edge/nginx.conf` (not the vendored copy under
`reference/`); `prometheus.yml` and `alertmanager.yml` are in `monitoring/` (the Grafana datasource is
`monitoring/grafana-provisioning/datasources/prometheus.yml`); `agent.conf` is `../../instraclustr/spire/agent.conf`;
`app.hcl` and `migrate.hcl` are in `../../instraclustr/vault-agent/`; "compose" is `docker-compose.yml` in this folder's parent
and "infra compose" is `../../instraclustr/docker-compose.yml`.

**Out to the internet** (the only three): litellm to the Anthropic API (the one provider, ADR 0005,
`litellm-config.yaml:21`), alertmanager to the Slack webhook (`alertmanager.yml:37`), and cloudflared to Cloudflare.
I found no other outbound HTTP in `src/`, `dashboard/` or `scripts/`: the only HTTP clients are rows 6 and 12.

**Needs no network at all** (talks through a shared volume, not a connection): the six `*-spiffe` helpers (they reach
spire-agent through the `nas-spire-agent-socket` volume and write to a memory-only volume), `edge-certs` (writes
certificates and exits), and the SWIM Kit (runs on the host and writes files into a bind mount). The application
containers do not talk to Vault either: they read secrets from a memory-only volume that their own Vault Agent fills.

**Paths open today that this plan closes:** an application container reaching Vault (it holds no credential and has a
sidecar to do it), SPIRE, Postgres directly, or another service's database; anything reaching spire-server except
spire-agent; anything reaching Temporal's port 7233 except the API, the worker and the UI; the application containers
reaching the unauthenticated admin screens.

**Not touched by this plan:** the ports published on the host's loopback (postgres 5432, pgbouncer 6432, vault 8200,
litellm 4000, temporal 7233, keycloak 8081, the edge's 8443-8448). They are a different door, controlled by the `ports:`
entries and held by `test_metrics_exposure.py` for the application services.

## The one rule that shapes the design

A Docker network is all or nothing between its members: it cannot allow port 8000 and refuse port 22. So a "who may talk
to whom" table becomes networks by finding groups where **every pair in the group is meant to talk**, and putting a
container on more than one network when it belongs to more than one group. Fewer, bigger networks are simpler and
leave more sideways movement; many small ones (down to pairs) are strict and are harder to run. So the plan is in
three tiers, each worth shipping on its own.

## Tier 1: wall off the secrets and the data (do this first)

| Network | Members | What it protects |
|---|---|---|
| `nas-spire` | spire-server, spire-agent | the identity authority: only its agent can reach it |
| `nas-vault` | vault, the six vault-agent sidecars, vault-unseal, vault-ceremony, litellm-bootstrap | the secrets: **no application container is on it** |
| `nas-db` | postgres, pgbouncer, migrate, proof, litellm, litellm-db-init, postgres-scram | the main database: the runtime services cannot reach it except through the pooler |
| `nas-pool` | pgbouncer, api, sentinel, temporal-worker, dashboard | the pooled application role |
| `nas-llm` | litellm, api, sentinel, temporal-worker, dashboard, proof, litellm-bootstrap | the model gateway and its master key |
| `nas-temporal-db` | temporal, temporal-postgresql | Temporal's database |
| `nas-keycloak-db` | keycloak, keycloak-postgres | Keycloak's database |
| `nas-app` | everything else: edge, cloudflared, dashboard, api, sentinel, temporal-worker, grafana, prometheus, alertmanager, temporal, temporal-ui, keycloak | the rest, still flat among themselves |

The six `*-spiffe` helpers move to no network (`network_mode: none`; to confirm in the rehearsal that spiffe-helper
needs nothing else, since it has no health check or other endpoint in the compose file).

**What Tier 1 buys:** a compromised web-facing or application container can no longer open a connection to Vault,
SPIRE, either database, or the gateway's master-key holder, except along the paths in rows 7 to 13. The databases and
Vault stop being one hop from everything.

**What it leaves:** inside `nas-app` everything still reaches everything, including the unauthenticated admin
screens (rows 4, 15) and Temporal's open port (row 14). Tier 2 closes those.

## Tier 2: the unauthenticated admin screens and Temporal

Replace the shared `nas-app` with small networks, so Temporal UI, Alertmanager and Prometheus are reachable only by
their one legitimate caller:

| Network | Members |
|---|---|
| `nas-front-dashboard` | edge, dashboard |
| `nas-front-api` | edge, api |
| `nas-front-keycloak` | edge, keycloak, dashboard (rows 5 and 6) |
| `nas-front-grafana`, `nas-front-temporal-ui`, `nas-front-prometheus`, `nas-front-alertmanager` | edge and that one service |
| `nas-temporal` | temporal, api, temporal-worker, temporal-ui |
| `nas-metrics` | prometheus, api, sentinel, temporal-worker, dashboard |
| `nas-metrics-temporal` | prometheus, temporal (so Prometheus never shares a network with Temporal's port 7233) |
| `nas-alerts` | prometheus, alertmanager |
| `nas-grafana-data` | grafana, prometheus |

**Accepted residual:** the four application services share `nas-metrics`, so they can still reach each other. They are
one image at one trust level and each can already reach the pooler; the strict alternative is one network per scraped
service (four more) if an assessor asks for it. The edge is the hub of every front network, so it remains the pivot
that matters, and it is the one container that should stay minimal.

## Tier 3: control what may go out

Give an internet-capable network (`nas-egress`) only to litellm, alertmanager and cloudflared, and make every other
network `internal: true`, so no application container can phone home. **Check in the rehearsal before relying on it:**
Docker may not publish a host port for a container attached only to internal networks (postgres, pgbouncer, vault,
litellm, temporal, keycloak and the edge all publish loopback ports). If so, those containers keep one non-internal
network and Tier 3 is partial. Optional, and the least valuable of the three.

## Things in this stack that make it harder than a normal compose file

- **Two compose files share one network and one volume** (`nas-network`, `nas-spire-agent-socket`), both external and
  created by [`launch_nas_sentinel.ps1`](../../launch_nas_sentinel.ps1#L59). The new networks must be created the same
  way, before either stack starts, and declared `external: true` in both files.
- **Containers find each other by name** (`nas-pgbouncer`, `postgres`, `vault`, `nas-litellm`, `temporal`, `keycloak`).
  A name resolves only on a network both share, so a missed membership shows up as "cannot resolve host", not a refusal.
- **spire-agent holds the Docker socket** (`instraclustr/docker-compose.yml:356`). Anything with Docker API access can attach
  a container to any network, so segmentation does not defend against that one container being taken over. Already
  in the threat model; this plan does not change it.
- **`depends_on` is start-up order, not a connection**: the dashboard waits for the API (`compose 292`) but never calls it,
  so no dashboard-to-API path is needed.
- **Recreating spire-agent or vault.** A changed network membership recreates the container. Vault comes back sealed and
  `vault-unseal` runs again by design. Your notes record that the SPIRE agent needed re-attesting after a long Docker
  stop; `agent.conf` says a quick restart reuses the cached node identity. The rehearsal must prove the quick case
  and the runbook for the slow one ([`../../instraclustr/README.md`](../../instraclustr/README.md), "SPIRE: first boot") must be open.
- **Things that name `nas-network` and must change with it:** `launch_nas_sentinel.ps1`; the CI step that creates it
  (`.github/workflows/pyapp-ci.yml:145`); `.github/scripts/deploy-smoke.sh` (it also **creates and deletes
  `nas-network` itself**, so it must be parameterised before it is used next to a live stack); the probes
  `scripts/smoke/02_negative_access_probe.sh` and `05_public_exposure_probe.sh`; the instructions in
  `src/storage/test_support.py`; `instraclustr/tests/test_public_access.py:67`; and the sentences in `operations.md`,
  `API_DEPLOYMENT.md`, `nginx.conf`, `signing.cnf` and the infra README.

## How it is proved

1. **A test that holds the table.** `instraclustr/tests/test_network_segmentation.py`, in the style of
   `test_metrics_exposure.py`: the rows above as data; compute which pairs of services share a network from both compose
   files; every allowed row must share one, and every pair in the "refused" list must share none. It runs in the normal
   infra suite, so a new service or a quiet change of network fails it.
2. **A live probe**, `scripts/smoke/06_network_segmentation_probe.sh`, in the style of probe 02: from a container on
   each zone, try the forbidden targets (must fail) and, for each, the allowed one beside it (must work), so a pass
   means "refused" and not "broken".
3. **A rehearsal on a scratch copy** of both stacks (different project and network names, different host ports) before
   the live cutover, including recreating spire-agent and vault, the Tier 3 internal-network question, and the
   no-network spiffe helpers.

## Order of work

0. **Observe the running stack.** For each container, list its established connections (from `/proc/net/tcp`, since the
   images have no `ss`) and map the addresses back to container names. Compare with the table; fix the table, not
   the stack. This is the step that turns "derived" into "observed".
1. Write the test and the probe against the table. Both fail on today's flat network, which is the point.
2. Rehearse Tier 1 on the scratch copy; fix what the rehearsal finds.
3. **Live, Tier 1, dual-homed first:** create the networks; add them to each service *while it stays on `nas-network`*
   (nothing is refused yet); confirm every service is healthy.
4. **Live, one network at a time** (spire, then vault, then the databases, then the gateway): remove `nas-network` from the
   affected services, run the probe and the smoke suite, stop if anything fails. Rolling back a step is adding
   `nas-network` back to those services and running `up -d`.
5. Delete `nas-network`; update the launcher, CI, probes, docs and the infra README's "Not done here" line.
6. Tier 2, then optionally Tier 3, the same way.

Each live step restarts the services it touches. **Mind the janitor:** the SWIM Kit keeps writing files on the host while
the watcher (`sentinel`) is down, and `scripts/Cleanup-KitLog.ps1`, which the launcher starts with the live feed,
deletes Kit files older than 5 minutes (`-MaxAgeMinutes`, default 5). Keep the watcher's restart well under that, or stop
the janitor for the duration, or messages that arrived during the restart can be deleted before they are read.

## Decisions for the owner

1. **Tier 1 first, then stop and look?** (Recommended.) Or Tiers 1 and 2 together.
2. **Is the shared `nas-metrics` network acceptable**, or should the four application services be separated too?
3. **Tier 3**: worth doing, or leave egress as it is? It is the weakest of the three and depends on the Docker behaviour above.
4. **Rehearsal on this machine**: it needs a second copy of both stacks running briefly on other host ports. Is that acceptable
   while the live stack runs?

## Not covered

Encryption between services (Postgres TLS, Temporal mTLS, TLS for Prometheus scrapes and the gateway: separate items);
authentication on Temporal and the admin screens; the Docker socket held by spire-agent; a compromised host; the SWIM
Kit and its host. Segmentation limits who can connect; it does not make the connections safe.
