#!/bin/sh
# The model gateway's start: its three secrets come from the file its Vault
# Agent sidecar renders (vault-agent/litellm.hcl, on a memory-only volume
# mounted read-only here), not from compose or .env. Waits for the file,
# exports what is in it, builds LiteLLM's DATABASE_URL, then runs the image's
# own entrypoint with the compose command (--config /app/config.yaml --port 4000).
#
# The values are exported to this process only: `docker inspect nas-litellm`
# does not show them, and `docker exec` does not inherit them.
set -eu
SECRETS=/vault/secrets/litellm.env

i=0
until [ -s "$SECRETS" ]; do
    i=$((i + 1)); [ "$i" -ge 60 ] && { echo "ERROR: $SECRETS was not rendered in 120 s (docker logs litellm-vault-agent)" >&2; exit 1; }
    sleep 2
done

# KEY=value lines, read literally: a value is never evaluated by the shell. The
# `|| [ -n "$line" ]` keeps a last line that has no newline.
while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        ANTHROPIC_API_KEY=*|LITELLM_MASTER_KEY=*|LITELLM_DB_PASSWORD=*) export "$line" ;;
    esac
done < "$SECRETS"
: "${LITELLM_MASTER_KEY:?$SECRETS has no LITELLM_MASTER_KEY}" "${LITELLM_DB_PASSWORD:?$SECRETS has no LITELLM_DB_PASSWORD}"
[ -n "${ANTHROPIC_API_KEY:-}" ] || echo "WARNING: ANTHROPIC_API_KEY is empty in Vault (secret/litellm): the gateway cannot reach the model, and the Critic reports itself unavailable" >&2

# Its own database, for virtual keys and the spend record (litellm-db-init.sh).
# Direct to Postgres: Prisma needs session-level connections, not the pooler.
# The password goes into the URL as it is, as it did from .env; a generated one
# is hex (vault-unseal-and-seed.bash), which needs no escaping.
export DATABASE_URL="postgresql://litellm:${LITELLM_DB_PASSWORD}@postgres:5432/litellm"
unset LITELLM_DB_PASSWORD
exec /app/docker/prod_entrypoint.sh "$@"
