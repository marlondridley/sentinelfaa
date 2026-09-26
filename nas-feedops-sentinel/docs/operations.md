# Operations

How to bring the stack up, prove it works, and run the live FAA feed.

## Start everything

From the repository root:

```powershell
.\launch_nas_sentinel.ps1              # infrastructure, then the application
.\launch_nas_sentinel.ps1 -WithLiveFeed  # also start the Jumpstart kit
```

The launcher creates `nas-network`, starts `instraclustr/` (Postgres,
PgBouncer, Vault, LiteLLM), waits for them, then starts this stack. In this
stack, `migrate` runs first and every application service waits for it to
finish successfully.

The `edge` nginx proxy is the only entry point: HTTPS, bound to `127.0.0.1`,
with a self-signed certificate for localhost written to `./tls/` by
`python -m src.devcert` on first start (kept across restarts). Trust
`tls/edge.crt` once in the Windows certificate store to silence the browser
warning, or replace the pair with a certificate from your CA.

| Service | URL |
|---|---|
| Dashboard | https://localhost:8443 |
| API | https://localhost:8444/health |
| Grafana | https://localhost:8445 |
| Temporal UI | https://localhost:8446 |
| Prometheus (and its alerts) | https://localhost:8447/alerts |
| Keycloak admin (HTTP, loopback) | http://localhost:8081/admin |

Internal traffic between containers stays plain HTTP on `nas-network`, which
nothing outside the host can reach. The API's `/metrics` and `/docs` are not
routed by the proxy.

## Prove it works

```bash
docker compose --profile proof run --rm proof
```

`scripts/e2e_proof.py` runs the whole path against a throwaway database (it
refuses any database whose name is not marked disposable) and prints one line
per step. CI runs the same script with the model disabled. See the script's
docstring for what each step asserts.

## Identities and secrets

* **Database.** `migrate` connects as the schema owner (Vault
  `secret/postgres/credentials`). Every other service connects as
  `sentinel_app` (Vault `secret/postgres/app`), which has DML on operational
  tables and only `SELECT`/`INSERT` on `action_audit_log` and
  `investigations`. `APP_DB_PASSWORD` must be the same in `.env` and
  `../instraclustr/.env`; the Vault seed job copies it into Vault.
* **Vault.** Services authenticate with AppRole, not a token. On every start
  `instraclustr/vault-unseal-and-seed.bash` unseals Vault (initializing it
  first after a `down -v`), seeds secrets, reconciles the `sentinel-app` and
  `sentinel-migrate` policies and roles, rotates their secret IDs, and writes
  `vault-init-data/approle/<role>/{role_id,secret_id}`. Each service mounts
  only its own role's directory; `sentinel-app` can read
  `secret/postgres/app` and `secret/sentinel/config` and nothing else. No
  manual `vault write` is ever needed.
* **Vault unseal and root.** The unseal key is split into 3 shares, any 2 of
  which unseal: share 1 in `instraclustr/vault-init-data/unseal/`, share 2
  in the `vault-unseal-share` Docker volume (mounted only into the unseal
  job), share 3 in `instraclustr/vault-recovery/`. **Move share 3 off this
  machine, then delete it.** No root token is stored: it exists in memory at
  first initialization only, long enough to create the `vault-bootstrap`
  AppRole the bootstrap jobs use (it can seed this project's secrets and
  manage its sentinel-* roles, nothing else), and is then revoked. For
  break-glass access, regenerate one from two shares with
  `vault operator generate-root`.
* **LLM proxy.** The application holds a LiteLLM virtual key, not the master
  key. `instraclustr/litellm-bootstrap.sh` issues it after the proxy is up
  (alias `sentinel-app`: `primary-model` and `gemini-backup` only, a 30-day
  budget `LITELLM_APP_BUDGET_USD`, `LITELLM_APP_RPM` requests/minute) and
  stores it in Vault. LiteLLM keeps keys in its own `litellm` database
  (`litellm-db-init.sh`).
* **Operators.** Keycloak realm roles decide who may act:
  `watch_officer` (approve / reject / monitor), `supervisor` (also override),
  `auditor` (read-only). A signed-in user with no role is read-only. Grant
  roles with `scripts/Setup-Keycloak.ps1` (new operators) or
  `scripts/keycloak-rbac.sh` with `GRANT_USERS` / `GRANT_ROLES`.
* **API.** `/v1/ingest` requires `API_KEY` (or a named key in `API_KEYS`) and
  refuses every call if none is configured.

## Ingest guarantees

* **Stable identities.** An event's id comes from its SWIM message
  (`mex:globalID:msgSeqID`, or the file name) plus its position in the
  message; an alert's from the rule and what triggered it. Re-dropping a
  file, a double scan or a retried run updates the same rows, never adds
  duplicates or a second staged action.
* **One consumer per file.** A scanner claims a file by renaming it into the
  folder's hidden `.inflight/` subfolder before reading it; only one rename
  can succeed. A claim left by a crashed scanner is released after 10
  minutes. A read-only folder is read in place.
* **Provenance.** Every event and alert carries `data_origin`: `live` (the
  kit's folder), `replay`, `synthetic` (sample button, demo scenario), `api`,
  or `unknown` for rows from before the tag. Alerts inherit it (`mixed` for
  aggregates spanning origins). The dashboard warns on every page when
  non-live data is present, the review queue labels non-live actions, and the
  Red-Team Critic challenges any alert that is not from the live feed.

## Escalation integrity

`nas-sentinel` runs a reconciler every `RECONCILE_INTERVAL_SECONDS` (60):
any CRITICAL alert without a staged action is staged PENDING if it is younger
than `RECONCILE_STAGE_WINDOW_MINUTES` (60), or recorded EXPIRED ("never staged
for review in time") if older. `nas_critical_alerts_unstaged` should always
read 0; the `CriticalAlertsUnstaged` alert fires if it does not. Run one pass
by hand with `python -m src.reconcile`.

## Alerts

Rules are in `monitoring/alert-rules.yml`: unstaged CRITICAL alerts, a stale
review queue, degraded airport feeds, no ingest for 30 minutes, a high model
error rate, and scrape targets down. Prometheus evaluates them and shows them
at https://localhost:8447/alerts. No Alertmanager is configured yet, so
nothing pages anyone.

## Models

`instraclustr/litellm-config.yaml` routes the `primary-model` alias to
DeepSeek with Gemini as fallback. `gemini-backup` has one deployment per
Gemini key (`GEMINI_API_KEY`, `_2`, `_3`); the proxy spreads calls across
them and benches a key that hits its quota (the free tier allows 20
requests/day per model per project). That is a low-cost test configuration.
Neither provider is an authorized boundary for FAA operational data; move
`primary-model` to an approved endpoint before real feed data reaches it.
Application settings name aliases only (`LLM_MODEL=primary-model`);
provider routes such as `gemini/...` are refused at startup.

## The FAA SWIM Jumpstart kit

Not containerized, deliberately. The kit ships a prebuilt fat jar
(`jumpstart/lib/jumpstart-jar-with-dependencies.jar`) and its own launchers;
containerizing it added a Maven/JDK 25 build that bought nothing. It runs
natively on the host (the launcher's `-WithLiveFeed` starts it with
`jumpstart/run-live.ps1`), or by hand:

```bat
cd jumpstart
mkdir ..\data\raw\swim_live\log
cd ..\data\raw\swim_live
..\..\..\jumpstart\bin\run.bat ^
    -DproviderUrl=%SWIM_PROVIDER_URL% -Dqueue=%SWIM_QUEUE% ^
    -DconnectionFactory=%SWIM_CONNECTION_FACTORY% ^
    -Dusername=%SWIM_USERNAME% -Dpassword=%SWIM_PASSWORD% ^
    -Dvpn=%SWIM_VPN% ^
    -Doutput=com.harris.cinnato.outputs.MessageFileOutput ^
    -Djson=true -Dheaders=true
```

* `MessageFileOutput` writes to `<cwd>/log/`, which is why the kit is
  launched from `data/raw/swim_live/`. `SWIM_RAW_FOLDER` points the watcher
  at the same directory.
* `-Djson=true -Dheaders=true` are required: the parser expects JMS property
  header lines followed by one JSON object.
* Requires a Java 25 runtime (`java -version` must report 25 or newer).
* Known limitations of the stock output: files named by epoch millisecond
  (two messages in the same millisecond overwrite each other) and non-atomic
  writes (mitigated by the watcher's 2 s debounce).
  `jumpstart/src/.../outputs/IngestFolderFileOutput.java` fixes both and takes
  `-DoutputDir`, but needs the jar rebuilt (JDK 25, `mvn -B -DskipTests clean package`).

## Volumes

Services that read the SWIM folder (`sentinel`, `temporal-worker`,
`dashboard`) bind-mount `./data` only. Never add a second volume at
`/app/data`: whichever mount wins at container creation sticks for the
container's life, and a service once read an empty named volume instead of
the live folder that way.
