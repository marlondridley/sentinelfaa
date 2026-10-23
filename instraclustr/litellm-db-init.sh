#!/bin/sh
# One-shot, every infrastructure start. Idempotent.
#
# LiteLLM needs its own database to issue virtual keys: without one it
# accepts only its master key (any other key fails with "No connected db"),
# which is why the application used to hold the proxy's admin credential.
# This creates a dedicated `litellm` login role and database on the shared
# Postgres server -- separate from sentinel_db and owning nothing in it.
#
# The role's password is Vault's (secret/litellm), read from the file
# litellm-vault-agent renders, mounted read-only here; setting it on every run
# is also how a rotated password reaches the role.
set -eu
: "${POSTGRES_USER:?}" "${POSTGRES_PASSWORD:?}"
SECRETS=/vault/secrets/litellm.env
LITELLM_DB_PASSWORD=$(sed -n 's/^LITELLM_DB_PASSWORD=//p' "$SECRETS")
: "${LITELLM_DB_PASSWORD:?no LITELLM_DB_PASSWORD in $SECRETS}"
export PGPASSWORD="$POSTGRES_PASSWORD"
PSQL="psql -h postgres -U $POSTGRES_USER -d postgres -v ON_ERROR_STOP=1 -q"

$PSQL -v pw="$LITELLM_DB_PASSWORD" <<'SQL'
SELECT 'CREATE ROLE litellm LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'litellm') \gexec
ALTER ROLE litellm PASSWORD :'pw';
SELECT 'CREATE DATABASE litellm OWNER litellm'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'litellm') \gexec
REVOKE ALL ON DATABASE litellm FROM PUBLIC;
SQL
echo "litellm database ready"
