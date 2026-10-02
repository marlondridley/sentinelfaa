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

The `edge` nginx proxy is the only entry point: HTTPS, bound to `127.0.0.1`.
On first start `python -m src.devcert` creates a local certificate authority
in `./tls/` (`edge-ca.crt`, kept across restarts) and signs two server
certificates with it: the edge proxy's (localhost) and Keycloak's (keycloak,
localhost, 127.0.0.1). Each is re-issued when missing or near expiry. Trust
`tls/edge-ca.crt` once in the Windows certificate store and renewals stay
trusted, or replace the pairs with certificates from your own CA.

Each container mounts one directory under `./tls/`, holding only what it
needs: `edge/` (nginx's pair), `keycloak/` (Keycloak's pair and the
authority's certificate), `trust/` (the authority's certificate, for the
dashboard). Only `edge-certs` sees the authority's key. The mounts are
directories, never single files: `docker compose up` creates every container
before `edge-certs` has run, and Docker creates a missing file-mount source
as an empty directory, which then blocks the file from being written.

| Service | URL |
|---|---|
| Dashboard | https://localhost:8443 |
| API | https://localhost:8444/health |
| Grafana | https://localhost:8445 |
| Temporal UI | https://localhost:8446 |
| Prometheus (and its alerts) | https://localhost:8447/alerts |
| Alertmanager (what was sent, silences) | https://localhost:8448 |
| Keycloak admin (HTTPS, loopback) | https://localhost:8081/admin |

Keycloak is the identity provider; Streamlit keeps the session. "Sign in
with Keycloak" sends the browser to the realm's own login page
(https://localhost:8081, the authorization code flow with PKCE S256). Keycloak
returns it to `https://localhost:8443/oauth2callback`, and the dashboard
exchanges the code at `https://keycloak:8443` server to server, verifying
Keycloak against `tls/edge-ca.crt` (`[auth.keycloak.client_kwargs] verify` in
`.streamlit/secrets.toml`). Streamlit validates the ID token and keeps the
identity in its signed, HttpOnly cookie, so a sign-in survives page loads and
new tabs. A sign-in lasts at most the realm's maximum SSO session (10 hours)
from authentication, and "Sign out" ends the realm session too. The password
is typed into Keycloak only; the client allows no direct access grant.
Identity URLs must be HTTPS: the dashboard refuses to start a sign-in
otherwise. Keycloak's health check runs over TLS too
(`keycloak/TlsHealth.java`; the image has no curl).

Other traffic between containers stays plain HTTP on `nas-network`, which
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
  `investigations`. Both passwords live in `../instraclustr/.env`, which the
  Vault seed job copies into Vault; this stack's `.env` holds none.
* **Workload identity and Vault.** No service holds a credential, a Vault
  token or a route to Vault. Each has two sidecars: a spiffe-helper that
  spire-agent attests (by its `com.sentinelfaa.identity` label) and issues
  the service's own one-hour X.509-SVID, `spiffe://sentinelfaa.internal/
  workload/<service>`; and a Vault Agent that logs in over mutual TLS with it
  and renders what the service's policy allows into
  `/vault/secrets/app-config.env`, on a memory-only volume the service
  mounts read-only. `sentinel-app` (api, dashboard, sentinel,
  temporal-worker) can read `secret/postgres/app` and
  `secret/sentinel/config` and nothing else; `sentinel-migrate` (migrate,
  proof) adds the owner's `secret/postgres/credentials`. Vault's audit log
  (`docker logs vault`) names the service behind every request. How it is
  wired, the first-time steps and the threat model:
  [`instraclustr/README.md`](../../instraclustr/README.md#workload-identity).
* **Certificates.** Vault serves TLS only, and SPIRE signs under an offline,
  passphrase-protected root: [`instraclustr/certs/README.md`](../../instraclustr/certs/README.md).
  The launcher checks them on every start and warns 60 days before renewal.
* **Vault unseal and root.** The unseal key is split into 3 shares, any 2 of
  which unseal: share 1 in `instraclustr/vault-init-data/unseal/`, share 2
  in the `vault-unseal-share` Docker volume (mounted only into the unseal
  job), share 3 in `instraclustr/vault-recovery/`. **Move share 3 off this
  machine, then delete it.** No root token is stored: it exists in memory at
  first initialization only, long enough to create the `vault-bootstrap`
  AppRole the bootstrap jobs use (it can seed this project's secrets, nothing
  else), and is then revoked. Policy and auth changes go through the root
  ceremony (`vault-ceremony`, `instraclustr/terraform/ceremony.sh`): a root
  token from shares 1 and 2, one Terraform run, revoked.
  `python instraclustr/scripts/vault_custody.py` checks all of this read-only:
  where each share is (share 3 still on this machine is a finding), that
  nothing under `vault-init-data/` or `terraform/` looks like a root token, and,
  with `--vault-addr` and a token you pass in the environment (the script's
  help names the variable), whether that token is live, root, or revoked. It never prints a secret and does not read `.env`.
* **LLM proxy.** The application holds a LiteLLM virtual key, not the master
  key. `instraclustr/litellm-bootstrap.sh` issues it after the proxy is up
  (alias `sentinel-app`: `primary-model` only, a 30-day
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
  can succeed. A claim does not touch the file's own timestamps (they are the
  message's arrival); its age is the modification time of a `.claim` marker
  beside it. A scan whose events could not be stored gives its files back at
  once, and a message whose data the database rejects (a number past 32 bits)
  is quarantined alone instead of holding the batch. A claim left by a crashed
  scanner is released after 4 minutes, shorter than the janitor's 5, so the file
  is scanned again before it can be dropped. A read-only folder is read in place.
* **Provenance.** Every event and alert carries `data_origin`: `live` (the
  kit's folder), `replay`, `synthetic` (test fixtures and the end-to-end proof; no run mode writes it), `api`,
  or `unknown` for rows from before the tag. Alerts inherit it (`mixed` for
  aggregates spanning origins). The dashboard warns on every page when
  non-live data is present, the review queue labels non-live actions, and the
  Red-Team Critic challenges any alert that is not from the live feed.
* **blockAtFAA.** A message whose `blockAtFAA` JMS property is `true` is
  withheld: nothing from it is stored, shown or sent to a model. The file is
  archived to `data/processed/`, logged in `ingest_log` with status
  `withheld`, and counted in `nas_swim_messages_withheld_total`. Any value
  other than `true`/`false` fails the file into `data/failed/`; an absent
  property is not a flag (the kit writes properties only with
  `-Dheaders=true`).
* **Beacon codes.** Per the FAA's STDDS beacon code notice (2017-10-11), a
  TAIS flight plan's beacon code is stored and shown only once the plan is
  active; Pending, Passive, missing or unknown status withholds it. A track
  with no flight plan keeps the code its own transponder broadcasts.

## Escalation integrity

`nas-sentinel` runs a reconciler every `RECONCILE_INTERVAL_SECONDS` (60):
any CRITICAL alert without a staged action is staged PENDING if it is younger
than `RECONCILE_STAGE_WINDOW_MINUTES` (60), or recorded EXPIRED ("never staged
for review in time") if older. `nas_critical_alerts_unstaged` should always
read 0; the `CriticalAlertsUnstaged` alert fires if it does not. Run one pass
by hand with `python -m src.reconcile`.

## Alerts

Rules are in `monitoring/alert-rules.yml`; each has a runbook in
`docs/runbooks.md`. They cover:
- unstaged CRITICAL alerts;
- a stale review queue;
- degraded airport feeds;
- no ingest for 30 minutes;
- model errors, both the legacy `nas_llm_*` series and the GenAI
  `error.type`;
- model p95 latency;
- agents falling back to rules;
- model calls refused by policy;
- an agent blocked from deciding or writing;
- scrape targets down;
- the alerting path itself: a heartbeat (`Watchdog`), Slack refusing
  notifications, and Prometheus unable to reach Alertmanager.

Prometheus evaluates them and shows them at https://localhost:8447/alerts, and
sends what fires to Alertmanager (`monitoring/alertmanager.yml`), which groups it
and posts it to Slack: one message per alert, a critical one after 10 seconds and
reminded hourly, the rest after 30 seconds and reminded every 4 hours, with a
"resolved" message when it clears. Alertmanager's own page, with silences, is
https://localhost:8448.

The destination is one variable, `SLACK_WEBHOOK_URL`, in `instraclustr/.env` or
`nas-feedops-sentinel/.env` (a Slack Incoming Webhook; both files are passed to the
stack). It is written to memory when Alertmanager starts and is in no image or
file. Like the other credentials of services outside the Vault plane it is in the
container's environment, so `docker inspect` shows it; moving it into Vault is the
same change as for Grafana's and Keycloak's. With the variable unset Alertmanager
still runs and says so: alerts are held and shown, and nothing is sent. Setup,
testing and what to do when Slack stops receiving are in `docs/runbooks.md`
("Alerts are not reaching Slack"). `python .github/scripts/check_alerting.py`
(run by CI) checks the rule and route files with `promtool` and `amtool` and
delivers a test alert through a real Alertmanager to a stand-in for Slack.

Grafana's "Agents" row charts the GenAI and AGT metrics.

## Agent governance, telemetry and evaluation

How the AI layer is governed, boundary by boundary, is
`docs/agent-governance.md`. Which component computes each metric is
`docs/agent-metrics.md`. Operationally:

* **Policies.** Both are Microsoft Agent Governance Toolkit (AGT) policies,
  deny by default:
  - `policies/model-use.yaml` decides every model call;
  - `policies/tool-use.yaml` decides every tool an agent uses.

  A policy that fails to load refuses every call. Changes are code changes;
  the tests pin every decision.
* **Telemetry.** OpenTelemetry (`src/telemetry.py`) with the GenAI
  conventions:
  - traces: `invoke_workflow`, `invoke_agent`, `chat` and `execute_tool`
    spans;
  - metrics: `gen_ai.client.*` and AGT's `agent.*`, on each service's
    `/metrics`;
  - AGT's audit log records of every policy decision.

  Set `OTEL_EXPORTER_OTLP_ENDPOINT` in `.env` to also send everything to an
  OTLP collector. `OTEL_SERVICE_NAME` names the service. No prompt or
  response text is on any span or record.
* **Evaluation and readiness.**
  - `python -m src.readiness` prints the verdict for the last 24 hours.
    `--hours N`, `--json` and `--gate` are available; `--gate` exits 0 only
    when READY.
  - The dashboard's Agent Evaluation & Governance page shows the same
    figures. Targets and per-investigation gate limits are in
    `policies/model-slos.yaml`.
* **Records.** `model_calls` (one row per model call), `investigations`
  (sealed) and `action_audit_log` are append-only. The application role may
  only SELECT and INSERT them.

## Models

`instraclustr/litellm-config.yaml` routes the `primary-model` alias to Claude
Haiku 4.5, direct from Anthropic. It is the only model and there is no
fallback; `instraclustr/tests/test_model_provider.py` holds it to that. When
it is unavailable the Critic reports itself unavailable and every alert is
still staged for a person. This is a test configuration, not an authorized
boundary for FAA operational data: move `primary-model` to an approved
endpoint before real feed data reaches it. Application settings name aliases
only (`LLM_MODEL=primary-model`); provider routes such as `anthropic/...` are
refused at startup.

**Retries, breaker and budget** (`src/llm_guard.py`, `src/llm_client.py`).
The gateway is the one layer that retries provider calls and falls back
(`router_settings` in `litellm-config.yaml`). The application client retries
only a request that never reached the gateway, at most `LLM_CONNECT_ATTEMPTS`
(2) attempts; Temporal never sees a model failure, because the Critic and the
investigations catch it and fall back to rules. After
`LLM_BREAKER_THRESHOLD` (5) consecutive gateway failures an alias's circuit
opens for `LLM_BREAKER_COOLDOWN_SECONDS` (120) and the Critic reports itself
unavailable (alert `ModelCircuitOpen`). One pipeline pass may spend
`LLM_TOKEN_BUDGET_PER_PASS` (250000) tokens; past it calls are refused (alert
`LLMPassBudgetExhausted`). Refused calls are recorded in `model_calls` as
`circuit_open` or `budget_exhausted` and are excluded from the success rate.

## The FAA SWIM Jumpstart kit

Not containerized, deliberately. The kit ships a prebuilt fat jar
(`jumpstart/lib/jumpstart-jar-with-dependencies.jar`) and its own launchers;
containerizing it added a Maven/JDK 25 build that bought nothing. It runs
natively on the host (the launcher's `-WithLiveFeed` starts it with
`jumpstart/run-live.ps1`). The edge stays a JVM JMS consumer: the SCDS Guideline
makes JMS the only authorized API, so there is no Python AMQP client to write.

The kit itself is not in this repository: its LICENSE is a restricted-rights
notice, so `.gitignore` excludes everything under `jumpstart/` except our two
files, `run-live.ps1` and `IngestFolderFileOutput.java`. After a fresh clone,
download the kit from SCDS and unpack it over `nas-feedops-sentinel/jumpstart/`
(it supplies `lib/jumpstart-jar-with-dependencies.jar`, which the launcher and the
writer's compile step need).

### What the JVM hands over

The stock kit acknowledges a message (JMS `AUTO_ACKNOWLEDGE`) when its output
returns, and its `onMessage` catches every exception. `MessageFileOutput` logs a
failed write and returns, so a disk that fails, fills or vanishes acknowledges
messages that were never saved, and the broker will not send them again. It also
names files by the millisecond and truncates on open, so two messages in one
millisecond overwrite each other.

`jumpstart/src/main/java/.../outputs/IngestFolderFileOutput.java` replaces it and
is what `run-live.ps1` launches. It writes one record per message,
`swim_<run>_<seq>_<arrival millis>.swimmsg`:

```json
{"format": "swim-raw/1", "message_id": "TDES:51190821:1874",
 "received_at": "2026-08-27T20:42:25.503Z",
 "headers": {"mex": "TDES", "tracon": "PCT", "...": "..."},
 "body": "{...the payload as JSON...}"}
```

* `message_id` is the SWIM identity `mex:globalID:msgSeqID` when the message has
  one, otherwise `sha256:<hex>` of the payload. Python computes it by the same
  rule (`src/raw_message.py`) and a test holds the two to it.
* The record is written to a hidden temp file, fsync'ed, and renamed into place, so
  the watcher never sees half a record.
* **If the record cannot be made durable the JVM halts before the output returns**,
  so the kit never acknowledges and the broker redelivers. `run-live.ps1` restarts
  the kit with a growing pause (2 s to 60 s). A payload that cannot be converted to
  JSON is not a delivery failure: the original text is kept with a `convert_error`
  and ingest quarantines it with that reason. Halting on it would redeliver it forever.
* It refuses to start without `-DoutputDir`, or with a directory it cannot write.
* Redelivery is harmless for a message with a SWIM identity: event ids are derived
  from it, so the same message is the same events. A message with no identity
  (none has been seen on the live feed) has only the hash to go on, and a redelivery
  would be stored again.

Durability ends at the folder, and the folder is pruned. The janitors
(`scripts/Cleanup-KitLog.ps1`, the Ingest page's purge) delete every file older than
a few minutes (default 5) whether or not it was ingested. That is deliberate load
shedding, so a backlog cannot bury the live picture, but it means a sentinel outage
longer than that drops records the broker has already been told were handled. Turn the
janitor off, or raise its age, when nothing may be dropped.

The distance to that cliff is measured: `nas_swim_folder_backlog_files` and
`nas_swim_folder_oldest_waiting_seconds` (the oldest waiting record, by its arrival time)
are set on every scan, and the alert `SwimBacklogNearJanitorAge` fires at 3 minutes,
before the default 5-minute janitor. `nas_swim_files_dropped_unprocessed_total` counts what
the Python janitor (the Ingest page's purge) deleted while it was still waiting; the
PowerShell janitor cannot report, so for it the oldest-waiting gauge is the only signal.

What was and was not shown: the kit's own `onMessage` was driven with fake JMS
messages (`tests/java/`, run by `tests/test_ingest_output_java.py` when a JDK is
present), and a write failure does halt the JVM before `onMessage` can return.

That result was also checked against a real broker (2026-09-27): a local Solace
PubSub+ Standard container (JNDI enabled, one exclusive queue with delivery-count
tracking on), the unmodified `com.harris.cinnato.Consumer` entry point, and
`IngestFolderFileOutput` -- the exact production classes and command line
`run-live.ps1` uses, not a stand-in. Note that the kit uses JMS `AUTO_ACKNOWLEDGE`,
not `CLIENT_ACKNOWLEDGE` (`SolaceConsumerV1.connect()`: `createSession(false,
Session.AUTO_ACKNOWLEDGE)`); the property is `JMSXDeliveryCount`, and the sequence
observed was:
  1. A message published while the JVM was connected was consumed and written
     durably; the record's `headers.JMSXDeliveryCount` read `"1"`.
  2. The output directory was replaced with a plain file while the JVM stayed
     connected. A second message was published; the JVM logged the write failure
     and halted (confirmed by the process disappearing, not by a printed message)
     before `onMessage` could return, so `AUTO_ACKNOWLEDGE` never fired. No record
     was written.
  3. The broker detected the dropped connection and kept the message spooled,
     unacknowledged (`deletedMsgCount` stayed 0; `highestMsgId` still counted it).
  4. A second Consumer JVM, pointed at a fresh writable directory, connected to
     the same (exclusive) queue and received the same message redelivered, with
     `headers.JMSXDeliveryCount` now `"2"` and the same `message_id`
     (`mex:globalID:msgSeqID` is unchanged by redelivery, so the event id a
     redelivery produces is identical) -- proving the durable-write/halt design
     against JMS redelivery semantics as the broker actually implements them, not
     only as the spec describes them.

Not covered: the FAA's own SCDS broker, its VPN/ACL/redelivery-limit policy, TLS,
and behavior under sustained load or a longer outage (the connection here dropped
in well under a second; a real network partition may take the broker's keepalive
timeout to notice). The setup used, including the SEMPv2 calls needed to get past
a default-shipped VPN pointed at a disabled RADIUS profile with JNDI turned off,
is in the session's scratch history, not this repo -- ask if it should become a
runnable script.

### Launching

`run-live.ps1` builds `jumpstart/lib/swim-ingest-output.jar` from the tracked writer
source (it needs a JDK, `javac` and `jar`, not only a JRE), then runs the kit. By hand:

```bat
cd jumpstart
javac -cp lib\jumpstart-jar-with-dependencies.jar -d build src\main\java\com\harris\cinnato\outputs\IngestFolderFileOutput.java
jar cf lib\swim-ingest-output.jar -C build .
java -cp lib\jumpstart-jar-with-dependencies.jar;lib\swim-ingest-output.jar ^
    -DproviderUrl=%SWIM_PROVIDER_URL% -Dqueue=%SWIM_QUEUE% ^
    -DconnectionFactory=%SWIM_CONNECTION_FACTORY% ^
    -Dusername=%SWIM_USERNAME% -Dconfig.file=%SWIM_SECRET_CONF% ^
    -Dvpn=%SWIM_VPN% ^
    -Doutput=com.harris.cinnato.outputs.IngestFolderFileOutput ^
    -DoutputDir=..\data\raw\swim_live\log ^
    -Djson=true -Dheaders=true ^
    com.harris.cinnato.Consumer
```

* `SWIM_RAW_FOLDER` points the watcher at `-DoutputDir`. The kit's own log
  (`messages.log`) goes to `<cwd>\log`, which `run-live.ps1` arranges to be the same
  folder; the watcher does not read it.
* Never pass the password as `-Dpassword=...`: a command-line argument is visible
  in the process list and is recorded by command-line process auditing (Windows
  event 4688, Sysmon event 1). Put `password = "..."` in a config file only you can
  read and name it with `-Dconfig.file=` (`SWIM_SECRET_CONF` above); `run-live.ps1`
  does this itself with a temporary owner-only file.
* `-Djson=true -Dheaders=true` are required: the record carries the JMS properties
  (the SWIM identity) and the payload as JSON.
* Requires a Java 25 runtime (`java -version` must report 25 or newer).
* Files the stock kit already wrote (extensionless, header lines then a JSON object)
  still ingest.

### The seam, and when to revisit Kafka

Python reads through a small source interface (`src/raw_message.py`): `poll` what is
waiting, `commit` a message once its events are stored, `quarantine` one that can never
succeed. Everything above it (parsing, the STDDS extractors, validation, storage) sees a
`RawMessage`: `{message_id, headers, body, received_at}`, and nothing about files. The folder
is one implementation (`FolderSource` in `src/swim_file_ingest.py`; claim by atomic rename,
archive on commit, `failed/` on quarantine). `commit` runs only after storage succeeds, so a
source that acknowledges there is at-least-once.

Kafka, or any broker between the JVM and Python, is a second implementation of those three
methods and a Kafka producer in the JVM output; nothing downstream changes
(`tests/test_ingest_source_seam.py` drives the pipeline from a source that is not a folder).
It is deliberately not built. Revisit it when a **second consumer** of the raw stream exists,
or a **second feed** (TFMS, TBFM) has to be ingested next to this one. Until then it adds a
cluster to run, and a second place a message can be lost, for nothing the folder does not do.

### Scope

What is in scope is configuration, not code. The default is the four Potomac TRACON airports
the system was verified on. To watch others, set the airports and their TRACON:

```
SUPPORTED_AIRPORTS=KJFK,KLGA,KEWR
SUPPORTED_TRACON=N90
```

`src/airports.py` holds where each airport is and which TRACON it belongs to (a handful
beyond PCT are listed); an airport it does not know still works, with no map marker. Events for
anything else are refused where they are built, and the extractors keep only in-scope airports.
Airports of several TRACONs can be listed together: the TRACONs in scope are those of the
listed airports plus `SUPPORTED_TRACON`, which is also the default for a record that names none.
Only PCT has ever been verified against a captured feed. Another TRACON's messages have the same
STDDS shapes, but until one is captured that is an expectation, not a fact. The dashboard's
product copy (titles, the login page) still says PCT.

## Volumes

Services that read the SWIM folder (`sentinel`, `temporal-worker`,
`dashboard`) bind-mount `./data` only. Never add a second volume at
`/app/data`: whichever mount wins at container creation sticks for the
container's life, and a service once read an empty named volume instead of
the live folder that way.
