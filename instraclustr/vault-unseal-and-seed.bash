#!/bin/sh
# Replaces the old vault-init.bash now that vault runs with real (non-dev,
# Raft-backed) storage instead of -dev mode. Safe to run on every single
# container start, not just the first:
#   - First ever boot (Vault uninitialized): runs `vault operator init`
#     once, writes the root token + the one unseal key to
#     /vault-init-data/init.json (a host-visible bind mount, see
#     docker-compose.yml -- NOT an opaque Docker volume, specifically so a
#     human, or Claude with file access to this machine, can read the root
#     token out of it afterward).
#   - Every boot after that (Vault initialized but sealed, since Raft
#     storage always starts sealed): reads that same file and unseals.
#   - Every boot, either way: re-seeds the same secrets the old
#     vault-init.bash always wrote. vault kv put is idempotent, so this is
#     harmless even when nothing has changed.
set -eu

: "${VAULT_ADDR:?Missing VAULT_ADDR in environment}"
: "${POSTGRES_USER:?Missing POSTGRES_USER in environment}"
: "${POSTGRES_PASSWORD:?Missing POSTGRES_PASSWORD in environment}"
: "${POSTGRES_DB:?Missing POSTGRES_DB in environment}"
: "${DEEPSEEK_API_KEY:?Missing DEEPSEEK_API_KEY in environment}"
: "${GEMINI_API_KEY:?Missing GEMINI_API_KEY in environment}"
: "${LITELLM_MASTER_KEY:?Missing LITELLM_MASTER_KEY in environment}"
: "${APP_DB_PASSWORD:?Missing APP_DB_PASSWORD in environment}"
APP_DB_USER="${APP_DB_USER:-sentinel_app}"

INIT_FILE=/vault-init-data/init.json
STATUS_FILE=/tmp/vault-status.json

echo "=== Waiting for Vault API to respond ==="
i=0
got_response=0
while [ "$i" -lt 30 ]; do
    vault status -format=json >"$STATUS_FILE" 2>/dev/null || true
    if grep -q '"initialized"' "$STATUS_FILE" 2>/dev/null; then
        got_response=1
        break
    fi
    i=$((i + 1))
    sleep 2
done
if [ "$got_response" -ne 1 ]; then
    echo "ERROR: Vault API never responded after 60s" >&2
    exit 1
fi

INITIALIZED=$(grep -o '"initialized": *[a-z]*' "$STATUS_FILE" | sed -E 's/.*: *//')

if [ "$INITIALIZED" != "true" ]; then
    echo "=== First-ever boot: initializing Vault ==="
    echo "Using a single key share -- this is a single-operator local setup, not"
    echo "a multi-person production deployment, so Shamir's multi-key splitting"
    echo "would just be friction with no real security benefit here."
    vault operator init -key-shares=1 -key-threshold=1 -format=json > "$INIT_FILE"
    chmod 600 "$INIT_FILE"
    echo "Root token + unseal key written to $INIT_FILE."
    echo "Back this file up somewhere outside this repo -- losing it with no other"
    echo "key share means everything ever stored in Vault is permanently unrecoverable."
else
    echo "=== Vault already initialized -- reusing $INIT_FILE ==="
fi

if [ ! -f "$INIT_FILE" ]; then
    echo "ERROR: Vault reports initialized=true but $INIT_FILE is missing on this" >&2
    echo "container -- cannot unseal without the original unseal key." >&2
    exit 1
fi

# `vault operator init -format=json` pretty-prints its output across multiple
# lines (the "unseal_keys_b64" key and its array value are on separate
# lines), so a single-line grep against the raw file silently matches
# nothing for it -- which is exactly what happened here: UNSEAL_KEY came out
# empty, and `vault operator unseal ""` fell through to Vault's interactive
# prompt path (the "file descriptor 0 is not a terminal" error). Flattening
# the whole file to one line first makes the extraction immune to whatever
# indentation/line-wrapping this or any future Vault version's JSON output
# uses, without needing jq (still not available in this base image).
FLAT_JSON=$(tr -d '\n\t ' < "$INIT_FILE")
UNSEAL_KEY=$(echo "$FLAT_JSON" | grep -o '"unseal_keys_b64":\["[^"]*"' | sed -E 's/.*"([^"]+)"$/\1/')
ROOT_TOKEN=$(echo "$FLAT_JSON" | grep -o '"root_token":"[^"]*"' | sed -E 's/.*"([^"]+)"$/\1/')

if [ -z "$UNSEAL_KEY" ] || [ -z "$ROOT_TOKEN" ]; then
    echo "ERROR: failed to extract unseal key and/or root token from $INIT_FILE" >&2
    echo "-- the file exists but its structure didn't match what this script expects." >&2
    exit 1
fi

vault status -format=json > "$STATUS_FILE" 2>/dev/null || true
SEALED=$(grep -o '"sealed": *[a-z]*' "$STATUS_FILE" | sed -E 's/.*: *//')
if [ "$SEALED" = "true" ]; then
    echo "=== Unsealing Vault ==="
    vault operator unseal "$UNSEAL_KEY" >/dev/null
else
    echo "=== Vault already unsealed ==="
fi

export VAULT_TOKEN="$ROOT_TOKEN"

echo "=== Ensuring KV v2 secrets engine is mounted at 'secret/' ==="
if vault secrets list -format=json | grep -q '"secret/":'; then
    echo "KV engine 'secret/' already enabled."
else
    echo "Mounting KV v2 engine at 'secret/'..."
    vault secrets enable -path=secret kv-v2
fi

echo "=== Seeding secrets (idempotent -- safe to re-run) ==="

vault kv put secret/postgres/credentials \
    username="${POSTGRES_USER}" \
    password="${POSTGRES_PASSWORD}" \
    database="${POSTGRES_DB}"

vault kv put secret/litellm \
    DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY}" \
    GEMINI_API_KEY="${GEMINI_API_KEY}" \
    LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY}"

vault kv put secret/litellm/deepseek \
    api_key="${DEEPSEEK_API_KEY}"

vault kv put secret/litellm/gemini \
    api_key="${GEMINI_API_KEY}"

# Consumed as LITELLM_API_KEY by src/vault_config.py.
#
# TEMPORARY: the application authenticates to the proxy with the proxy's
# master key. The proxy has no key database, so it accepts no other key (a
# non-master key fails with "No connected db"). The least-privilege version
# gives LiteLLM a database and a virtual key scoped to the approved aliases
# with a budget; the master key is proxy admin and must not stay here.
vault kv put secret/sentinel/config \
    LITELLM_API_KEY="${LITELLM_MASTER_KEY}"

# The application's own database identity: DML on operational tables,
# SELECT/INSERT only on the evidence tables. Created and granted by the
# migrate job (src/storage/migrate.py); read by application services via
# VAULT_POSTGRES_SECRET_PATH=postgres/app. postgres/credentials above stays
# the schema owner, used only by that job.
vault kv put secret/postgres/app \
    username="${APP_DB_USER}" \
    password="${APP_DB_PASSWORD}" \
    database="${POSTGRES_DB}"

echo "=== Vault unseal + seed complete ==="
echo "Root token is saved in $INIT_FILE if you need it for anything manual."
