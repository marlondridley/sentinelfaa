#!/bin/sh
# One-shot, every infrastructure start. Idempotent.
#
# LiteLLM needs its own database to issue virtual keys: without one it
# accepts only its master key (any other key fails with "No connected db"),
# which is why the application used to hold the proxy's admin credential.
# This creates a dedicated `litellm` login role and database on the shared
# Postgres server -- separate from sentinel_db and owning nothing in it.
set -eu
: "${POSTGRES_USER:?}" "${POSTGRES_PASSWORD:?}" "${LITELLM_DB_PASSWORD:?}"
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
