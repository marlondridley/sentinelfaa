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

Everything is published on `127.0.0.1` only:

| Service | URL |
|---|---|
| Dashboard | http://localhost:8501 |
| API | http://localhost:8000/health |
| Keycloak | http://localhost:8081 |
| Grafana | http://localhost:3000 |
| Prometheus | http://localhost:9090 |
| Temporal UI | http://localhost:8088 |

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
* **LLM proxy.** The application authenticates to LiteLLM with the proxy's
  master key (`secret/sentinel/config`). This is temporary: give LiteLLM a
  database and issue a virtual key scoped to the approved aliases.
* **Operators.** Keycloak realm roles decide who may act:
  `watch_officer` (approve / reject / monitor), `supervisor` (also override),
  `auditor` (read-only). A signed-in user with no role is read-only. Grant
  roles with `scripts/Setup-Keycloak.ps1` (new operators) or
  `scripts/keycloak-rbac.sh` with `GRANT_USERS` / `GRANT_ROLES`.
* **API.** `/v1/ingest` requires `API_KEY` (or a named key in `API_KEYS`) and
  refuses every call if none is configured.

## Models

`instraclustr/litellm-config.yaml` routes the `primary-model` alias to
DeepSeek with Gemini as fallback. That is a low-cost test configuration.
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
