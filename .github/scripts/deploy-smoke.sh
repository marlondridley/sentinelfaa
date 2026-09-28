#!/usr/bin/env bash
# Deployment smoke test: the failures unit tests cannot see.
#
# Both of these passed the full test suite and still kept the stack from
# starting, because they live in how the containers are created and wired,
# not in the Python:
#
#   1. A fresh checkout. `docker compose up` creates every container before
#      the one-shot edge-certs job runs, and Docker creates a missing
#      file-mount source as an empty directory. With the certificates mounted
#      file by file, that left directories where the keys belong, edge-certs
#      failed, and Keycloak, the API, the dashboard and the edge never started.
#
#   2. An upgrade. A database initialised with md5 passwords, then started
#      with PgBouncer on SCRAM, fails every login with "cannot do SCRAM
#      authentication: wrong password type" -- while PgBouncer's own health
#      check (pg_isready, which never logs in) still reports healthy.
#      postgres-scram must migrate that database, and be safe to run again.
#
# Runs in CI on a clean runner. The compose files pin container names and
# loopback ports, so it refuses to run beside a live stack, and it needs a
# checkout with no nas-feedops-sentinel/tls/ (a fresh clone, or a worktree).
#
# Usage: bash .github/scripts/deploy-smoke.sh
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
INFRA_ENV="$ROOT/instraclustr/.env.example"
APP_ENV="$ROOT/nas-feedops-sentinel/.env.example"
TLS="$ROOT/nas-feedops-sentinel/tls"
P=sentinel-smoke
OLD_PG=sentinel-smoke-md5-postgres
PG_IMAGE=pgvector/pgvector:pg16          # the image instraclustr's postgres service runs
created_network=0
created_socket_volume=0
SOCKET_VOLUME=nas-spire-agent-socket   # external, like nas-network; launch_nas_sentinel.ps1 creates both

infra() { docker compose -p "$P-infra" -f "$ROOT/instraclustr/docker-compose.yml" --env-file "$INFRA_ENV" "$@"; }
app() { docker compose -p "$P-app" -f "$ROOT/nas-feedops-sentinel/docker-compose.yml" --env-file "$APP_ENV" "$@"; }
envval() { grep -E "^$2=" "$1" | head -1 | cut -d= -f2- | tr -d '\r'; }
step() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

wait_healthy() {  # container, seconds
  local status=""
  for _ in $(seq 1 "$2"); do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$1" 2>/dev/null || true)"
    [ "$status" = healthy ] && return 0
    sleep 1
  done
  fail "$1 is not healthy after $2 s (last status: ${status:-none})"
}

cleanup() {
  local code=$?
  if [ "$code" -ne 0 ]; then
    step "logs (failed with exit $code)"
    docker logs "$OLD_PG" 2>&1 | tail -n 40 || true
    infra logs --no-color --tail 60 2>/dev/null || true
    app logs --no-color --tail 60 2>/dev/null || true
  fi
  docker rm -f "$OLD_PG" >/dev/null 2>&1 || true
  app down -v --remove-orphans >/dev/null 2>&1 || true
  infra down -v --remove-orphans >/dev/null 2>&1 || true
  [ "$created_network" = 1 ] && docker network rm nas-network >/dev/null 2>&1 || true
  [ "$created_socket_volume" = 1 ] && docker volume rm "$SOCKET_VOLUME" >/dev/null 2>&1 || true
  exit "$code"
}

# ── Preconditions ────────────────────────────────────────────────────────────
for name in postgres postgres-scram nas-pgbouncer nas-keycloak nas-keycloak-postgres nas-edge-certs; do
  if docker ps -a --format '{{.Names}}' | grep -qx "$name"; then
    fail "container '$name' exists: stop the running stack first (the compose files pin container names and ports)"
  fi
done
[ ! -e "$TLS" ] || fail "$TLS exists: run from a fresh checkout (git worktree add), which is what this test is about"
trap cleanup EXIT
if ! docker network inspect nas-network >/dev/null 2>&1; then
  docker network create nas-network >/dev/null
  created_network=1
fi
# spire-agent's Workload API socket, shared by both stacks: every application
# service's spiffe-helper sidecar mounts it, so creating the app's containers
# needs it to exist, as the launcher makes sure on a real host.
if ! docker volume inspect "$SOCKET_VOLUME" >/dev/null 2>&1; then
  docker volume create "$SOCKET_VOLUME" >/dev/null
  created_socket_volume=1
fi

PG_USER="$(envval "$INFRA_ENV" POSTGRES_USER)"
PG_PASSWORD="$(envval "$INFRA_ENV" POSTGRES_PASSWORD)"
PG_DB="$(envval "$INFRA_ENV" POSTGRES_DB)"
APP_USER="$(envval "$INFRA_ENV" APP_DB_USER)"
APP_PASSWORD="$(envval "$INFRA_ENV" APP_DB_PASSWORD)"
LITELLM_PASSWORD="$(envval "$INFRA_ENV" LITELLM_DB_PASSWORD)"

# psql as a login over TCP, the way the services connect.
login() {  # host, port, user, password
  docker run --rm --network nas-network -e PGPASSWORD="$4" "$PG_IMAGE" \
    psql -h "$1" -p "$2" -U "$3" -d "$PG_DB" -v ON_ERROR_STOP=1 -Atc 'SELECT 1'
}
as_owner() {  # SQL -> single value, as the schema owner on the running server
  docker exec -e PGPASSWORD="$PG_PASSWORD" postgres psql -h 127.0.0.1 -U "$PG_USER" -d "$PG_DB" -Atc "$1"
}

# ── 1. An md5 database upgrades to SCRAM ─────────────────────────────────────
step "an install from before SCRAM: data directory initialised with md5, login roles hashed md5"
infra create postgres >/dev/null         # the named volume, created the way compose will reuse it
docker run -d --name "$OLD_PG" -v "$P-infra_postgres-data:/var/lib/postgresql/data" \
  -e POSTGRES_USER="$PG_USER" -e POSTGRES_PASSWORD="$PG_PASSWORD" -e POSTGRES_DB="$PG_DB" \
  -e POSTGRES_HOST_AUTH_METHOD=md5 -e POSTGRES_INITDB_ARGS=--auth=md5 "$PG_IMAGE" >/dev/null
wait_ready=0
for _ in $(seq 1 60); do
  if docker exec "$OLD_PG" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1 \
     && docker exec -e PGPASSWORD="$PG_PASSWORD" "$OLD_PG" psql -U "$PG_USER" -d "$PG_DB" -Atc 'SELECT 1' >/dev/null 2>&1; then
    wait_ready=1; break
  fi
  sleep 1
done
[ "$wait_ready" = 1 ] || fail "the md5 postgres did not start"
docker exec -i -e PGPASSWORD="$PG_PASSWORD" "$OLD_PG" psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -q \
  -v owner="$PG_USER" -v owner_pw="$PG_PASSWORD" -v app="$APP_USER" -v app_pw="$APP_PASSWORD" -v llm_pw="$LITELLM_PASSWORD" <<'SQL'
SET password_encryption = 'md5';
ALTER ROLE :"owner" PASSWORD :'owner_pw';
CREATE ROLE :"app" LOGIN PASSWORD :'app_pw';
CREATE ROLE litellm LOGIN PASSWORD :'llm_pw';
SQL
md5_roles="$(docker exec -e PGPASSWORD="$PG_PASSWORD" "$OLD_PG" psql -U "$PG_USER" -d "$PG_DB" -Atc \
  "SELECT count(*) FROM pg_authid WHERE rolcanlogin AND rolpassword LIKE 'md5%'")"
[ "$md5_roles" = 3 ] || fail "setup: expected 3 md5 login roles, found $md5_roles"
docker stop "$OLD_PG" >/dev/null && docker rm "$OLD_PG" >/dev/null

step "upgrade: postgres, then postgres-scram, then PgBouncer (as the launcher starts them)"
infra up -d pgbouncer                    # fails here if postgres-scram does not exit 0
wait_healthy nas-pgbouncer 120
scram_log="$(docker logs postgres-scram 2>&1)"
for expected in "re-hashed $PG_USER" "re-hashed $APP_USER" "re-hashed litellm" "pg_hba.conf switched from md5 to scram-sha-256"; do
  grep -qF "$expected" <<<"$scram_log" || fail "postgres-scram did not report '$expected':
$scram_log"
done
[ "$(as_owner "SELECT count(*) FROM pg_authid WHERE rolcanlogin AND rolpassword NOT LIKE 'SCRAM-SHA-256\$%'")" = 0 ] \
  || fail "a login role is still not hashed with SCRAM"
[ "$(docker exec postgres grep -Ec '^[^#]*[[:space:]]md5([[:space:]]|$)' /var/lib/postgresql/data/pg_hba.conf || true)" = 0 ] \
  || fail "pg_hba.conf still has md5 rules"

step "logins over SCRAM: through PgBouncer (the path that failed), and each migrated role directly"
[ "$(login nas-pgbouncer 6432 "$PG_USER" "$PG_PASSWORD")" = 1 ] || fail "login through PgBouncer failed"
[ "$(login postgres 5432 "$APP_USER" "$APP_PASSWORD")" = 1 ] || fail "the application role cannot log in"
[ "$(login postgres 5432 litellm "$LITELLM_PASSWORD")" = 1 ] || fail "the litellm role cannot log in"

step "postgres-scram again: nothing left to do, and it still exits 0"
rerun="$(infra run --rm --no-deps postgres-scram 2>&1)" || fail "postgres-scram failed on a second run:
$rerun"
if grep -qE "re-hashed|switched" <<<"$rerun"; then fail "postgres-scram changed something on a second run:
$rerun"; fi

# ── 2. A fresh checkout starts Keycloak over TLS ─────────────────────────────
step "a fresh checkout: every container created before edge-certs runs"
app create --build >/dev/null            # what `up` does first: all containers, all mounts
app up -d keycloak                       # keycloak-postgres, edge-certs, keycloak
[ "$(docker inspect -f '{{.State.ExitCode}}' nas-edge-certs)" = 0 ] || fail "edge-certs did not exit 0"
for f in edge-ca.crt edge/edge.crt edge/edge.key keycloak/keycloak.crt keycloak/keycloak.key keycloak/edge-ca.crt trust/edge-ca.crt; do
  [ -f "$TLS/$f" ] || fail "tls/$f is not a file"
done
stray="$(find "$TLS" -type d \( -name '*.crt' -o -name '*.key' \))"
[ -z "$stray" ] || fail "directories where certificate files belong: $stray"
wait_healthy nas-keycloak 300

step "the dashboard's path: verify Keycloak against tls/trust, mounted as the dashboard mounts it"
status="$(docker run --rm --network nas-network -v "$TLS/trust:/run/tls:ro" nas-feedops-sentinel:latest python -c '
import ssl, urllib.request
ctx = ssl.create_default_context(cafile="/run/tls/edge-ca.crt")
url = "https://keycloak:8443/realms/nas-sentinel/.well-known/openid-configuration"
print(urllib.request.urlopen(url, context=ctx, timeout=10).status)')"
[ "$status" = 200 ] || fail "Keycloak did not verify against tls/trust/edge-ca.crt (got: $status)"

step "deployment smoke passed"
